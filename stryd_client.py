"""
Stryd PowerCenter API client (api.stryd.com/b/api/v1).

Auth: JWT access token (~12h) refreshed via a rotating refresh token.
Refresh call mirrors the PowerCenter web app:
  POST /b/token/refresh  body={refresh_token, user_id}  header Client-ID
Response rotates the refresh token, so we persist the new one each time.

Config (config.ini [Stryd]):
  token, refresh_token, refresh_client_id, athlete_id
"""
import base64
import configparser
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

API = "https://api.stryd.com/b/api/v1"
REFRESH_URL = "https://api.stryd.com/b/token/refresh"


class StrydClient:
    def __init__(self, config_path=None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "config.ini")
        self.config_path = config_path
        self._cfg = configparser.ConfigParser()
        self._cfg.read(config_path)
        if not self._cfg.has_section("Stryd"):
            raise ValueError("No [Stryd] section in config.ini")
        s = self._cfg["Stryd"]
        self.token = s.get("token")
        self.refresh_token = s.get("refresh_token")
        self.client_id = s.get("refresh_client_id")
        self.user_id = s.get("athlete_id")

    # ── auth ──────────────────────────────────────────────────────

    @staticmethod
    def _jwt_exp(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        except Exception:
            return 0

    def _save_tokens(self):
        # Re-read from disk and touch only our keys: another process (e.g.
        # the Strava client) may have rewritten config.ini since we loaded
        # it, and clobbering its snapshot would lose rotated tokens.
        cfg = configparser.ConfigParser()
        cfg.read(self.config_path)
        if not cfg.has_section("Stryd"):
            cfg.add_section("Stryd")
        cfg["Stryd"]["token"] = self.token
        cfg["Stryd"]["refresh_token"] = self.refresh_token
        cfg["Stryd"]["refresh_client_id"] = self.client_id
        with open(self.config_path, "w") as f:
            cfg.write(f)
        self._cfg = cfg

    def _refresh(self):
        r = requests.post(
            REFRESH_URL,
            json={"refresh_token": self.refresh_token, "user_id": self.user_id},
            headers={"Client-ID": self.client_id},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        self.token = d["access_token"]
        rt = d.get("refresh_token")
        if isinstance(rt, dict) and rt.get("token"):
            self.refresh_token = rt["token"]
        elif isinstance(rt, str) and rt:
            self.refresh_token = rt
        self._save_tokens()
        logger.info("Stryd token refreshed")

    def _ensure_token(self):
        if self._jwt_exp(self.token) < time.time() + 120:
            self._refresh()

    def _get(self, path, params=None):
        self._ensure_token()
        r = requests.get(f"{API}{path}", params=params,
                         headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        if r.status_code == 401:
            self._refresh()
            r = requests.get(f"{API}{path}", params=params,
                             headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    # ── data ──────────────────────────────────────────────────────

    def get_activities(self, start_epoch, end_epoch):
        """Calendar activities in [start_epoch, end_epoch] (unix seconds)."""
        data = self._get(f"/users/{self.user_id}/calendar",
                         {"from": int(start_epoch), "to": int(end_epoch),
                          "include_deleted": "false"})
        if isinstance(data, dict):
            data = data.get("activities", [])
        return [a for a in data if a.get("timestamp")]

    def find_activity(self, start_epoch, tolerance_sec=1800):
        """The activity whose timestamp is closest to start_epoch, or None."""
        acts = self.get_activities(start_epoch - tolerance_sec, start_epoch + tolerance_sec)
        best, best_d = None, None
        for a in acts:
            d = abs(a["timestamp"] - start_epoch)
            if best_d is None or d < best_d:
                best, best_d = a, d
        return best

    def latest_cp(self):
        """Most recent critical power value (watts), or None.

        The history endpoint is not reliably ordered — pick by max created.
        """
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = "2020-01-01"
        hist = self._get(f"/users/{self.user_id}/cp/history",
                         {"startDate": start, "endDate": end})
        if isinstance(hist, list) and hist:
            newest = max(hist, key=lambda h: h.get("created", 0))
            return newest.get("critical_power")
        return None

    def metrics_for(self, start_epoch):
        """Stryd metrics for the run starting near start_epoch.

        Returns dict with rss (Running Stress Score), avg_power, ftp/cp,
        and derived power_zone info, or None if no match.
        """
        a = self.find_activity(start_epoch)
        if not a:
            return None
        return {
            "rss": a.get("stress"),
            "avg_power": a.get("average_power"),
            "ftp": a.get("ftp"),
            "cp": self.latest_cp(),
            "name": a.get("name"),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = StrydClient()
    print("CP:", c.latest_cp())
    import sys
    now = int(time.time())
    acts = c.get_activities(now - 7 * 86400, now)
    print(f"{len(acts)} activities in last 7d")
    for a in acts[-5:]:
        print(" ", datetime.fromtimestamp(a["timestamp"]), a.get("name"),
              f"RSS={a.get('stress'):.0f}" if a.get("stress") else "",
              f"P={a.get('average_power'):.0f}W" if a.get("average_power") else "")
