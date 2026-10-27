#!/usr/bin/env python3
"""
奢侈品补货监控（Van Cleef & Arpels 香港官网）

用法：
    python monitor.py                 # 正常检测一次
    python monitor.py --test-email    # 只发一封测试邮件，不打开网页
    python monitor.py --test-bark     # 只推送一条测试 Bark 警报，不打开网页
    python monitor.py --headed        # 本地调试：显示浏览器窗口

退出码：
    0  检测完成（有货并已成功通知，或确认无货），或测试发送成功
    1  页面上「有货按钮」和「没货按钮」都看不到（或页面打不开），已保存截图到 debug/
    2  配置缺失或通知发送失败（例如密码填错）

所有密码 / key 都从环境变量读取（在 GitHub Actions 里由 Secrets 注入）：
    BARK_KEY            Bark App 里的设备 key
    GMAIL_ADDRESS       用来发信的 Gmail 地址
    GMAIL_APP_PASSWORD  Gmail「应用专用密码」（16 位）
    MAIL_TO             收件邮箱（不填则发给 GMAIL_ADDRESS 自己）
可选环境变量：
    PRODUCT_URL         商品链接（不填用下方默认值）
    IN_STOCK_TEXTS      有货按钮文字，多个用 | 分隔
    OUT_OF_STOCK_TEXTS  没货按钮文字，多个用 | 分隔
    BARK_SERVER         自建 Bark 服务器地址（默认 https://api.day.app）
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_URL = (
    "https://www.vancleefarpels.com/hk/zh/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
)

# 香港站中文页面可能是繁体，也可能是简体，两种写法都认
DEFAULT_IN_STOCK_TEXTS = ["添加至购物袋", "添加至購物袋"]
DEFAULT_OUT_OF_STOCK_TEXTS = ["透过电话订购", "透過電話訂購", "通过电话订购"]

DEBUG_DIR = Path("debug")
HKT = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_ERROR = 2


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_list(name: str, default: list[str]) -> list[str]:
    raw = env(name)
    if not raw:
        return default
    return [t.strip() for t in raw.split("|") if t.strip()]


def now_hkt() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_hkt()} HKT] {msg}", flush=True)


def write_summary(markdown: str) -> None:
    """在 GitHub Actions 的运行页面上显示一行结果摘要。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


# ---------------------------------------------------------------------------
# 页面检测
# ---------------------------------------------------------------------------

# 在浏览器里执行：找出所有「可点击元素」，看文字是否匹配，并判断是否真正可见。
# 两个按钮在 HTML 里一直同时存在，所以绝不能只搜文字。
VISIBILITY_JS = r"""
({inStock, outOfStock}) => {
  const norm = s => (s || '').replace(/\s+/g, '');
  const inT = inStock.map(norm), outT = outOfStock.map(norm);

  function isVisible(el) {
    if (!el.isConnected) return false;

    // 1) 尺寸：宽或高几乎为 0 → 不可见（含 display:none、屏幕阅读器专用 1px 元素）
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;

    // 2) 浏览器原生判断（会检查祖先元素的 display / opacity / visibility）
    if (typeof el.checkVisibility === 'function') {
      const ok = el.checkVisibility({
        checkOpacity: true, checkVisibilityCSS: true,
        opacityProperty: true, visibilityProperty: true,
      });
      if (!ok) return false;
    }

    // 3) 手动兜底：沿祖先链检查 display:none / opacity:0 / hidden 属性
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none') return false;
      if (parseFloat(cs.opacity) === 0) return false;
      if (n.hasAttribute('hidden')) return false;
    }

    // 4) 元素自身的 visibility（会继承，所以只看自己）
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;

    // 5) 被裁剪成 0 或被移到页面外（例如 left:-9999px）
    if (cs.clip === 'rect(0px, 0px, 0px, 0px)') return false;
    if (cs.clipPath === 'inset(50%)') return false;
    if (r.right + window.scrollX <= 0 || r.bottom + window.scrollY <= 0) return false;

    return true;
  }

  const selector = 'button, a, [role="button"], input[type="submit"], input[type="button"]';
  const found = { inStock: [], outOfStock: [] };

  for (const el of document.querySelectorAll(selector)) {
    const text = norm(el.innerText || el.textContent || el.value || '');
    if (!text) continue;
    const kind = inT.some(t => text.includes(t)) ? 'inStock'
               : outT.some(t => text.includes(t)) ? 'outOfStock' : null;
    if (!kind) continue;
    found[kind].push({
      tag: el.tagName.toLowerCase(),
      text: text.slice(0, 40),
      visible: isVisible(el),
    });
  }

  return {
    inStockVisible: found.inStock.some(x => x.visible),
    outOfStockVisible: found.outOfStock.some(x => x.visible),
    inStockCount: found.inStock.length,
    outOfStockCount: found.outOfStock.length,
    details: found,
    title: document.title,
  };
}
"""


def save_debug(page, name: str) -> Path | None:
    """保存整页截图和 HTML，方便排查。"""
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(HKT).strftime("%Y%m%d-%H%M%S")
    shot = DEBUG_DIR / f"{name}-{stamp}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        log(f"已保存截图：{shot}")
    except Exception as e:  # noqa: BLE001
        log(f"截图失败：{e}")
        shot = None
    try:
        (DEBUG_DIR / f"{name}-{stamp}.html").write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return shot


def check_stock(url: str, in_texts: list[str], out_texts: list[str],
                timeout_s: int = 45, headed: bool = False) -> tuple[str, Path | None, dict]:
    """
    返回 (状态, 截图路径, 原始检测结果)
    状态：'in_stock' / 'out_of_stock' / 'unknown'
    """
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="zh-HK",
            timezone_id="Asia/Hong_Kong",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        result: dict = {}

        try:
            log(f"打开页面：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            try:
                # 等页面里的脚本把库存状态渲染完；有些网站永远不会「完全空闲」，超时就继续
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWError:
                pass

            # 轮询：有货按钮一出现就立即判定；没货按钮需要连续 3 次稳定可见才判定，
            # 避免页面刚加载时先显示「电话订购」、随后才切换成「添加至购物袋」的误判。
            deadline = time.time() + timeout_s
            out_streak = 0
            while True:
                result = page.evaluate(VISIBILITY_JS, {"inStock": in_texts, "outOfStock": out_texts})
                if result["inStockVisible"]:
                    break
                out_streak = out_streak + 1 if result["outOfStockVisible"] else 0
                if out_streak >= 3 or time.time() > deadline:
                    break
                time.sleep(1.5)

            log(
                "检测结果：有货按钮 找到 %d 个 / 可见=%s；没货按钮 找到 %d 个 / 可见=%s；页面标题=%r"
                % (result["inStockCount"], result["inStockVisible"],
                   result["outOfStockCount"], result["outOfStockVisible"], result["title"])
            )

            if result["inStockVisible"]:
                shot = save_debug(page, "in-stock")
                return "in_stock", shot, result
            if result["outOfStockVisible"]:
                return "out_of_stock", None, result

            title = (result.get("title") or "").lower()
            if "access denied" in title or "attention required" in title or "just a moment" in title:
                log("提示：页面标题像是被网站的反爬虫拦截了，请查看截图确认。")
            shot = save_debug(page, "unknown")
            return "unknown", shot, result

        except Exception as e:  # noqa: BLE001
            log(f"页面加载/检测出错：{type(e).__name__}: {e}")
            shot = save_debug(page, "error")
            return "unknown", shot, result
        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def send_bark(title: str, body: str, url: str = "") -> None:
    """
    Bark 紧急警报：level=critical 可以穿透静音和睡眠/勿扰模式；
    call=1 会让铃声重复响 30 秒；volume=10 为最大音量。
    """
    key = env("BARK_KEY")
    if not key:
        raise ConfigError("缺少 BARK_KEY（请在 GitHub Secrets 里添加）")
    server = env("BARK_SERVER", "https://api.day.app").rstrip("/")

    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "level": "critical",
        "volume": 10,
        "call": "1",
        "sound": "alarm",
        "group": "VCA补货",
    }
    if url:
        payload["url"] = url  # 点通知直接打开商品页

    req = urllib.request.Request(
        f"{server}/push",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Bark 返回 HTTP {e.code}：{detail}") from e

    if data.get("code") != 200:
        raise RuntimeError(f"Bark 推送失败：{data}")
    log("Bark 紧急警报已发送 ✅")


def send_email(subject: str, body: str, attachment: Path | None = None) -> None:
    sender = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD").replace(" ", "")  # 应用专用密码带不带空格都行
    to = env("MAIL_TO") or sender
    if not sender or not password:
        raise ConfigError("缺少 GMAIL_ADDRESS 或 GMAIL_APP_PASSWORD（请在 GitHub Secrets 里添加）")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    if attachment and attachment.exists():
        msg.add_attachment(attachment.read_bytes(), maintype="image",
                           subtype="png", filename=attachment.name)

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
            s.login(sender, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            "Gmail 登录失败：请确认 GMAIL_ADDRESS 正确、GMAIL_APP_PASSWORD 是 16 位「应用专用密码」"
            "（不是 Gmail 登录密码），且账号已开启两步验证。"
        ) from e
    log(f"邮件已发送到 {to} ✅")


def notify_in_stock(url: str, shot: Path | None) -> bool:
    """两条通知互不影响：一条失败，另一条照样发。返回是否全部成功。"""
    title = "🍀 VCA 补货了！"
    body = f"Vintage Alhambra 五花手链现在可以加入购物袋，快去下单！\n检测时间：{now_hkt()}（香港时间）\n{url}"
    ok = True
    for name, fn in (
        ("Bark", lambda: send_bark(title, "五花手链有货了，点我直接打开商品页", url)),
        ("邮件", lambda: send_email(title, body, shot)),
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"❌ {name} 通知失败：{e}")
    return ok


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="VCA 补货监控")
    ap.add_argument("--test-email", action="store_true", help="只发送测试邮件")
    ap.add_argument("--test-bark", action="store_true", help="只发送测试 Bark 紧急警报")
    ap.add_argument("--url", default=env("PRODUCT_URL", DEFAULT_URL), help="商品链接")
    ap.add_argument("--timeout", type=int, default=45, help="页面等待秒数")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（本地调试用）")
    args = ap.parse_args()

    if args.test_email or args.test_bark:
        try:
            if args.test_email:
                send_email(
                    "✅ VCA 补货监控：测试邮件",
                    f"如果你看到这封邮件，说明 Gmail 设置正确。\n发送时间：{now_hkt()}（香港时间）",
                )
            if args.test_bark:
                send_bark("✅ VCA 补货监控：测试警报", "如果手机在静音状态下也响了，说明设置正确", args.url)
            write_summary("✅ 测试通知已发送")
            return EXIT_OK
        except Exception as e:  # noqa: BLE001
            log(f"❌ 测试失败：{e}")
            write_summary(f"❌ 测试失败：{e}")
            return EXIT_ERROR

    in_texts = env_list("IN_STOCK_TEXTS", DEFAULT_IN_STOCK_TEXTS)
    out_texts = env_list("OUT_OF_STOCK_TEXTS", DEFAULT_OUT_OF_STOCK_TEXTS)

    status, shot, _ = check_stock(args.url, in_texts, out_texts, args.timeout, args.headed)

    if status == "in_stock":
        log("🎉 有货！正在发送通知……")
        ok = notify_in_stock(args.url, shot)
        write_summary("🎉 **有货！** " + ("通知已发送" if ok else "但有通知发送失败，请看日志"))
        return EXIT_OK if ok else EXIT_ERROR

    if status == "out_of_stock":
        log("暂无现货（可见的是「透过电话订购」按钮）。")
        write_summary("⏳ 暂无现货")
        return EXIT_OK

    log("⚠️ 两个按钮都看不到，无法判断。截图已保存到 debug/ 文件夹。")
    write_summary("⚠️ 两个按钮都看不到，请下载 Artifacts 里的截图查看")
    return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
