#!/usr/bin/env python3
"""Screenshot the fleet dashboard with headless chromium.

Grafana's own /render endpoint needs the image-renderer plugin, which is a
~400 MB sidecar every local user would then pull. Grafana here is anonymous and
already serves the dashboard as its home, so a browser is enough, and the
dependency stays in CI rather than in the generated stack.
"""
import argparse
import sys

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="Grafana base URL, e.g. http://localhost:3000")
    ap.add_argument("-o", "--out", default="grafana.png")
    ap.add_argument("--width", type=int, default=1920)
    # Deliberately tall, not the 1080 you would use for a browser. Grafana
    # lazy-loads panels: anything outside the viewport never issues its query,
    # and a full_page screenshot captures those panels still empty -- it grows
    # the image without making Grafana think they are visible. Making the
    # viewport itself tall enough for the whole dashboard is what gets every
    # panel to load.
    ap.add_argument("--height", type=int, default=5200)
    # Panels render from Prometheus queries that fire after first paint; a
    # networkidle wait returns before the graphs have data on them.
    ap.add_argument("--settle", type=float, default=8,
                    help="seconds to let panels finish querying before capture")
    # Should match how long the fleet has actually been up. Too wide and the
    # run is a spike at the right edge of otherwise empty charts; this is also
    # the range Grafana prints in the top-right, so a matched window makes the
    # screenshot state the period it covers.
    ap.add_argument("--window", default="15m",
                    help="dashboard time range, as a Grafana offset (e.g. 6m)")
    args = ap.parse_args()

    # kiosk drops the nav chrome.
    url = (f"{args.url.rstrip('/')}/?kiosk"
           f"&from=now-{args.window}&to=now&refresh=5s")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        try:
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(int(args.settle * 1000))
            # The viewport is taller than the dashboard on purpose (see above),
            # so crop to the last panel rather than shipping the slack below it.
            bottom = page.evaluate(
                """() => {
                    const panels = document.querySelectorAll('.react-grid-item');
                    if (!panels.length) return null;
                    return Math.max(...[...panels].map(
                        p => p.getBoundingClientRect().bottom));
                }"""
            )
            clip = None
            if bottom and bottom + 8 < args.height:
                clip = {"x": 0, "y": 0,
                        "width": args.width, "height": int(bottom) + 8}
            page.screenshot(path=args.out, full_page=clip is None, clip=clip)
        finally:
            browser.close()

    print(f"wrote {args.out} from {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
