import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch an Airbnb listing page and save the embedded script payload as JSONL without parsing it."
    )
    parser.add_argument("url", help="Airbnb listing URL to inspect")
    parser.add_argument(
        "--output",
        "-o",
        default="airbnb_unit_raw.jsonl",
        help="Path to the output JSONL file (default: airbnb_unit_raw.jsonl)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=8.0,
        help="Seconds to wait for the page and script content to load",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30000,
        help="Page load timeout in milliseconds",
    )
    return parser.parse_args()


def clean_script_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("No script content was found.")

    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        text = text[9:-3]

    for prefix in [
        "window.__INITIAL_STATE__ = ",
        "var initialState = ",
        "const initialState = ",
        "let initialState = ",
    ]:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    return text.rstrip().rstrip(";")


def parse_json_payload(raw_text: str) -> Any:
    cleaned = clean_script_text(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        for start_char in ("{", "["):
            start_index = cleaned.find(start_char)
            if start_index == -1:
                continue
            stack: list[str] = []
            in_string = False
            escape = False
            for index in range(start_index, len(cleaned)):
                char = cleaned[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char in "{[":
                    stack.append(char)
                elif char in "}]":
                    if not stack:
                        break
                    stack.pop()
                    if not stack:
                        try:
                            return json.loads(cleaned[start_index:index + 1])
                        except json.JSONDecodeError:
                            break
            break
        raise ValueError(f"Unable to parse script JSON: {exc}") from exc


async def extract_script_payload(url: str, wait_seconds: float = 8.0, timeout_ms: int = 30000) -> str:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(int(wait_seconds * 1000))

            selectors = [
                "script#data-deferred-state-0",
                "script[data-state='true']",
                "script[data-state=\"true\"]",
            ]
            script_text = None

            for selector in selectors:
                try:
                    script_locator = page.locator(selector)
                    if await script_locator.count() > 0:
                        script_text = await script_locator.first.inner_text()
                        if script_text and script_text.strip():
                            break
                except Exception:
                    continue

            if not script_text:
                script_candidates = page.locator("script")
                count = await script_candidates.count()
                for index in range(count):
                    candidate_text = await script_candidates.nth(index).inner_text()
                    if not candidate_text:
                        continue
                    if any(marker in candidate_text for marker in ["niobeClientData", "niobeMinimalClientData", "stayProductDetailPage"]):
                        script_text = candidate_text
                        break

            if not script_text:
                raise RuntimeError(
                    "No Airbnb script payload was found on the page.")

            return script_text
        finally:
            await browser.close()


def write_jsonl(payload: Any, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False,
                     indent=2, sort_keys=True))
        handle.write("\n")


async def main() -> None:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()

    try:
        payload_text = await extract_script_payload(
            args.url,
            wait_seconds=args.wait_seconds,
            timeout_ms=args.timeout_ms,
        )
        payload = parse_json_payload(payload_text)
        write_jsonl(payload, output_path)
        print(f"Wrote raw parsed Airbnb payload to {output_path}")
    except Exception as exc:
        error_payload = {
            "url": args.url,
            "error": str(exc),
            "type": type(exc).__name__,
        }
        write_jsonl(error_payload, output_path)
        print(f"Extraction failed: {exc}")
        print(f"Wrote failure details to {output_path}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
