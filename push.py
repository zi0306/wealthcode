"""Web Push：手機鎖屏、App 關掉也收得到訊號通知。
VAPID 金鑰優先讀環境變數 VAPID_PRIVATE / VAPID_PUBLIC；沒有就自動產生並存在 DATA_DIR（主機重建會換金鑰，客戶需重按一次「開啟提示通知」）。"""
import os, json, base64, threading
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

class Push:
    def __init__(self, path):
        self.path, self.lock, self.subs = path, threading.Lock(), {}
        try: self.subs = json.load(open(path))
        except Exception: pass
        priv, pub = os.getenv("VAPID_PRIVATE"), os.getenv("VAPID_PUBLIC")
        if not (priv and pub):
            kp = path + ".vapid"
            try: priv, pub = json.load(open(kp))
            except Exception:
                k = ec.generate_private_key(ec.SECP256R1())
                priv = _b64(k.private_numbers().private_value.to_bytes(32, "big"))
                pub = _b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
                try: json.dump([priv, pub], open(kp, "w"))
                except Exception: pass
        self.private_key, self.public_key = priv, pub
        self.subject = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")

    def _save(self):
        try: json.dump(self.subs, open(self.path, "w"))
        except Exception as e: print("推播訂閱儲存失敗", e)

    def add(self, key, sub):
        with self.lock:
            lst = self.subs.setdefault(key, [])
            if sub["endpoint"] not in [x["endpoint"] for x in lst]: lst.append(sub)
            self._save()

    def send(self, key, msg, price=None):
        from pywebpush import webpush, WebPushException
        with self.lock: lst = list(self.subs.get(key, []))
        body = json.dumps({"title": "黃鼎彥的財富密碼", "body": msg + (f"｜{price:g}" if price else "")}, ensure_ascii=False)
        ok, dead = 0, []
        for s in lst:
            try:
                webpush(s, body, vapid_private_key=self.private_key, vapid_claims={"sub": self.subject}, ttl=300)
                ok += 1
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410): dead.append(s["endpoint"])
                else: print("推播失敗", e)
            except Exception as e: print("推播失敗", e)
        if dead:
            with self.lock:
                self.subs[key] = [x for x in self.subs.get(key, []) if x["endpoint"] not in dead]; self._save()
        return ok

    def send_async(self, key, msg, price=None):
        threading.Thread(target=self.send, args=(key, msg, price), daemon=True).start()
