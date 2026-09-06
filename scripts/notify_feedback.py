"""
Telegram feedback prompt for a newly synced activity.

Sends a message with inline buttons via the OpenClaw CLI: confirm/correct the
workout type, pick shoes (when the gear cache exists), and — for quality
sessions — a prompt to reply with lactate values. Button callbacks are handled
by the OpenClaw agent, which runs apply_feedback.py.

Callback protocol:
  runfb:<activity_id>:type:<Type>
  runfb:<activity_id>:gear:<gear_id>
"""
import configparser
import json
import logging
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LT_DIR = os.path.dirname(SCRIPT_DIR)
GEAR_CACHE = os.path.join(LT_DIR, "strava_gear.json")

log = logging.getLogger("auto_sync")

TYPES = ["Easy", "Moderate", "Threshold", "Interval", "Long Run", "Recovery", "Race"]


def _chat_target():
    c = configparser.ConfigParser()
    c.read(os.path.join(LT_DIR, "config.ini"))
    return (c.get("Telegram", "chat_id", fallback=None),
            c.get("Telegram", "channel", fallback="telegram"))


def _btn(label, value, style=None):
    b = {"label": label, "action": {"type": "callback", "value": value}}
    if style:
        b["style"] = style
    return b


def notify_new_run(aid, title, wtype, dist_km, avg_power, pct_cp, rss, analysis=""):
    target, channel = _chat_target()
    if not target:
        return

    stats = f"{dist_km:.1f} km · {avg_power or '?'}W ({pct_cp or '?'}% CP) · RSS {rss or '?'}"
    text = f"🆕 Синканув ран: {title}\n{stats}"
    if analysis:
        text += f"\n\n🧠 {analysis}"
    text += f"\n\nТип визначив як [{wtype}]. Кнопки нижче — або відпиши текстом:"

    # Text-reply fallback (works even when inline-button callbacks are gated)
    type_menu = " · ".join(f"{i+1} {t}" for i, t in enumerate(TYPES))
    reply_help = (f"\n\n✍️ Відповідь текстом:\n"
                  f"• тип: {type_menu}\n"
                  f"• кросівки: г + номер (напр. г3)")
    if wtype in ("Threshold", "Interval"):
        reply_help += "\n• лактат: напр. лактат 6: 2.8, 10: 3.8"

    blocks = [{"type": "text", "text": text}]

    type_btns = [_btn("✅ Ок", f"runfb:{aid}:type:ok", "success")]
    type_btns += [_btn(t, f"runfb:{aid}:type:{t}") for t in TYPES if t != wtype]
    # rows of 4
    for i in range(0, len(type_btns), 4):
        blocks.append({"type": "buttons", "buttons": type_btns[i:i + 4]})

    if os.path.exists(GEAR_CACHE):
        try:
            with open(GEAR_CACHE) as f:
                shoes = json.load(f)
            shoe_btns = [_btn(f"👟 {s['name'][:24]}", f"runfb:{aid}:gear:{s['id']}")
                         for s in shoes[:8]]
            if shoe_btns:
                blocks.append({"type": "text", "text": "Кросівки:"})
                for i in range(0, len(shoe_btns), 2):
                    blocks.append({"type": "buttons", "buttons": shoe_btns[i:i + 2]})
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    if wtype in ("Threshold", "Interval"):
        blocks.append({"type": "text",
                       "text": "🧪 Якщо міряв лактат — відповідай текстом, "
                               "наприклад: лактат 6: 2.8, 10: 3.8"})

    # Append the text-reply help to the leading text block
    blocks[0]["text"] = text + reply_help

    try:
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", channel, "--target", target,
             "-m", text + reply_help,
             "--presentation", json.dumps({"blocks": blocks}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        log.info("  telegram feedback prompt sent")
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("  telegram notify failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    notify_new_run(sys.argv[1], sys.argv[2], sys.argv[3],
                   float(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7]))
