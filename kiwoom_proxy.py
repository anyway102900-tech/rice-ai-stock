"""
키움증권 REST API 로컬 프록시 서버 (kiwoom_proxy.py)
------------------------------------------------------
용도: 브라우저(HTML)에서 키움증권 REST API를 직접 호출할 때 발생하는
      CORS(Cross-Origin Resource Sharing) 차단을 우회하고,
      AppKey와 SecretKey를 이용해 실시간 시세를 받아옵니다.

실행 방법:
    python kiwoom_proxy.py
    (기본 포트: 5000 -> http://localhost:5000)
"""

import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import ssl
import sys
import time

PORT = 5000

# 키움증권 REST API 기본 도메인
KIWOOM_REAL_DOMAIN = "https://openapi.kiwoom.com"
KIWOOM_MOCK_DOMAIN = "https://openapi.kiwoom.com"

# 발급된 액세스 토큰 메모리 캐시 { (appkey, mode): (token, expire_time) }
TOKEN_CACHE = {}


def get_kiwoom_token(appkey: str, secretkey: str, is_mock: bool = False):
    """키움증권 OAuth2 액세스 토큰 발급 (또는 캐시된 유효 토큰 반환)"""
    cache_key = (appkey, is_mock)
    now = time.time()

    if cache_key in TOKEN_CACHE:
        token, expire_at = TOKEN_CACHE[cache_key]
        if now < expire_at - 60:  # 만료 1분 전까지 재사용
            return token, None

    base_url = KIWOOM_MOCK_DOMAIN if is_mock else KIWOOM_REAL_DOMAIN
    url = f"{base_url}/oauth2/token"

    payload = json.dumps({
        "grant_type": "client_credentials",
        "appkey": appkey,
        "appsecret": secretkey
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "KiwoomProxy/1.0"
    }

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8, context=ctx) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            token = res_data.get("access_token")
            expires_in = int(res_data.get("expires_in", 86400))
            if token:
                TOKEN_CACHE[cache_key] = (token, now + expires_in)
                return token, None
            return None, res_data.get("error_description", "토큰 발급 실패")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, f"연결 오류: {str(e)}"


def fetch_kiwoom_stock_price(code: str, token: str, appkey: str, secretkey: str, is_mock: bool = False):
    """키움증권 REST API로 국내주식 현재가 조회"""
    base_url = KIWOOM_MOCK_DOMAIN if is_mock else KIWOOM_REAL_DOMAIN
    
    # 키움 REST API 주식현재가 시세 URL
    url = f"{base_url}/api/dpt/stock/current-price?code={code}"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": appkey,
        "appsecret": secretkey,
        "tr_id": "FHKST01010100",  # 주식현재가 시세
        "User-Agent": "KiwoomProxy/1.0"
    }

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=8, context=ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
            output = data.get("output", {})
            curr_price = float(output.get("stck_prpr", 0) or output.get("price", 0))
            change = float(output.get("prdy_vrss", 0) or output.get("change", 0))
            rate = float(output.get("prdy_ctrt", 0) or output.get("rate", 0))
            volume = int(output.get("acml_vol", 0) or output.get("volume", 0))

            return {
                "market": "KR",
                "code": code,
                "currentPrice": curr_price,
                "change": change,
                "changeRate": rate,
                "volume": volume,
                "source": "kiwoom_real"
            }, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return None, f"키움 시세조회 HTTP {e.code}: {body}"
    except Exception as e:
        return None, f"키움 시세조회 오류: {str(e)}"


def fetch_fallback_krx_price(code: str):
    """키움 API 미입력 또는 연결 오류 시 실시간 시세 폴백 (실시간 시세 보조)"""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
            # 네이버 모바일 API 필드 매핑
            raw_price = data.get("closePrice") or data.get("nowVal") or "0"
            raw_change = data.get("compareToPreviousClosePrice") or data.get("changeVal") or "0"
            raw_rate = data.get("fluctuationsRatio") or data.get("changeRate") or "0"

            curr_price = float(str(raw_price).replace(",", "").strip() or 0)
            change = float(str(raw_change).replace(",", "").strip() or 0)
            rate = float(str(raw_rate).replace(",", "").strip() or 0)

            return {
                "market": "KR",
                "code": code,
                "currentPrice": curr_price,
                "change": change,
                "changeRate": rate,
                "volume": 0,
                "source": "fallback_realtime"
            }, None
    except Exception as e:
        return None, f"시세 조회 실패: {str(e)}"


class KiwoomProxyHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # 헬스체크 / 테스트 연결 엔드포인트
        if parsed.path == "/api/health" or parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            res = {"status": "ok", "message": "키움증권 로컬 프록시가 정상 작동 중입니다."}
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            return

        # 시세 조회 엔드포인트
        if parsed.path == "/api/kiwoom-price":
            code = params.get("code", [""])[0]
            market = params.get("market", ["KR"])[0]
            appkey = params.get("appkey", [""])[0].strip()
            secretkey = params.get("secretkey", [""])[0].strip()
            mode = params.get("mode", ["real"])[0]
            is_mock = (mode == "mock")

            if not code:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "종목 코드가 필요합니다."}, ensure_ascii=False).encode("utf-8"))
                return

            result = None
            err_msg = None

            # 1) AppKey/SecretKey가 제공된 경우 -> 키움증권 REST API 정식 호출 시도
            if appkey and secretkey:
                token, tok_err = get_kiwoom_token(appkey, secretkey, is_mock)
                if token:
                    result, err_msg = fetch_kiwoom_stock_price(code, token, appkey, secretkey, is_mock)
                else:
                    err_msg = f"키움 토큰발급 실패: {tok_err}"

            # 2) 키가 없거나 키움 API 실패 시 -> 실시간 데이터 폴백으로 안정적 지원
            if not result:
                fallback_result, fb_err = fetch_fallback_krx_price(code)
                if fallback_result:
                    result = fallback_result
                    if err_msg:
                        result["notice"] = f"키움 API 연결 미비({err_msg})로 실시간 대체 시세를 제공합니다."
                else:
                    err_msg = err_msg or fb_err

            if result:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            else:
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": err_msg or "시세 조회 실패"}, ensure_ascii=False).encode("utf-8"))
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()

    def log_message(self, format, *args):
        # 콘솔에 간결하게 로그 출력
        sys.stderr.write(f"[KiwoomProxy] {self.address_string()} - {args[0]} {args[1]} -> {args[2]}\n")


def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), KiwoomProxyHandler) as httpd:
        print(f"==================================================")
        print(f"🚀 키움증권 REST API 프록시 서버가 시작되었습니다.")
        print(f"📡 주소: http://localhost:{PORT}")
        print(f"⚡ 브라우저에서 '실시간 가격 가져오기'를 누르면 자동 연동됩니다.")
        print(f"   (종료하려면 Ctrl+C 를 누르세요)")
        print(f"==================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n프록시 서버를 종료합니다.")


if __name__ == "__main__":
    run_server()
