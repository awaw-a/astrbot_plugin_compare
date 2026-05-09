import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("compare", "aw4w", "调用 LLM 生成两者优劣对照表并输出图片", "1.0.1")
class ComparePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cache_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_compare"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @filter.command("compare", alias={"比较", "对比", "谁强"})
    async def compare(self, event: AstrMessageEvent):
        """比较两个对象，并以本地 PNG 表格输出结果。"""
        try:
            left, right = self._parse_items(event.message_str)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        yield event.plain_result(f"正在比较「{left}」和「{right}」，我先问问模型。")

        try:
            data = await self._generate_compare_data(event, left, right)
            image_path = self._render_png(data)
            yield event.image_result(str(image_path))
        except ImportError:
            yield event.plain_result(
                "缺少 Pillow 依赖，无法本地生成图片。请在 AstrBot 控制台安装 Pillow，"
                "或确认插件目录里有 requirements.txt 后重载插件。"
            )
        except Exception as exc:
            logger.exception("生成对比图片失败")
            yield event.plain_result(f"生成对比失败：{exc}")

    def _parse_items(self, raw_message: str) -> tuple[str, str]:
        text = raw_message.strip()
        text = re.sub(r"^[/!！]?(compare|比较|对比|谁强)\s*", "", text, flags=re.I).strip()
        text = re.sub(r"[?？。!！]+$", "", text).strip()

        patterns = [
            r"^(.+?)\s*(?:vs\.?|VS|Vs|和|与|跟|同|比|,|，|/|\|)\s*(.+)$",
            r"^(.+?)谁(?:更|比较)?(?:强|厉害|好|优秀)\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, text)
            if match:
                left = match.group(1).strip(" 「」『』[]()（）")
                right = match.group(2).strip(" 「」『』[]()（）")
                if left and right and left != right:
                    return left, right

        raise ValueError(
            "用法：/compare 对象A vs 对象B\n"
            "也可以写：/比较 对象A 和 对象B，例如 /比较 Python 和 Java"
        )

    async def _generate_compare_data(
        self, event: AstrMessageEvent, left: str, right: str
    ) -> dict[str, Any]:
        provider_id = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        if not provider_id:
            raise RuntimeError("当前会话没有可用的 LLM 提供商，请先在 AstrBot 配置模型。")

        prompt = self._build_prompt(left, right)
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        payload = self._load_json(llm_resp.completion_text)
        return self._normalize_data(payload, left, right)

    def _build_prompt(self, left: str, right: str) -> str:
        return f"""
你是一个客观、幽默但不胡编的群聊裁判。请比较「{left}」和「{right}」谁更厉害。

要求：
1. 从 5 到 7 个维度比较，两边都要给出优点和短板。
2. 适合群聊阅读，句子短，有信息量，不要写成长文。
3. 如果两者不是同一领域，也要说明比较口径，避免绝对化。
4. 只输出 JSON，不要 Markdown，不要代码块。

JSON 格式必须是：
{{
  "title": "{left} vs {right}",
  "summary": "一句话总评",
  "winner": "整体更占优的一方；如果很难比较，写 各有胜场",
  "left": "{left}",
  "right": "{right}",
  "rows": [
    {{
      "aspect": "比较维度",
      "left_good": "左侧优点",
      "left_bad": "左侧短板",
      "right_good": "右侧优点",
      "right_bad": "右侧短板"
    }}
  ],
  "final": "最后给群友看的结论"
}}
""".strip()

    def _load_json(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S | re.I)
        if fenced:
            cleaned = fenced.group(1).strip()
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start : end + 1]
        return json.loads(cleaned)

    def _normalize_data(
        self, payload: dict[str, Any], left: str, right: str
    ) -> dict[str, Any]:
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("LLM 没有返回可用的对比 rows")

        normalized_rows = []
        for row in rows[:7]:
            normalized_rows.append(
                {
                    "aspect": str(row.get("aspect", "综合表现"))[:24],
                    "left_good": str(row.get("left_good", ""))[:80],
                    "left_bad": str(row.get("left_bad", ""))[:80],
                    "right_good": str(row.get("right_good", ""))[:80],
                    "right_bad": str(row.get("right_bad", ""))[:80],
                }
            )

        return {
            "title": str(payload.get("title") or f"{left} vs {right}")[:40],
            "summary": str(payload.get("summary") or "")[:120],
            "winner": str(payload.get("winner") or "各有胜场")[:40],
            "left": str(payload.get("left") or left)[:30],
            "right": str(payload.get("right") or right)[:30],
            "rows": normalized_rows,
            "final": str(payload.get("final") or "")[:140],
        }

    def _render_png(self, data: dict[str, Any]) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        self._cleanup_image_cache()

        width = 1200
        margin = 42
        gap = 18
        aspect_w = 150
        side_w = (width - margin * 2 - aspect_w) // 2
        x0 = margin
        x1 = x0 + aspect_w
        x2 = x1 + side_w
        x3 = x2 + side_w

        font_title = self._load_font(ImageFont, 42, bold=True)
        font_h2 = self._load_font(ImageFont, 24, bold=True)
        font_head = self._load_font(ImageFont, 21, bold=True)
        font_body = self._load_font(ImageFont, 19)
        font_small = self._load_font(ImageFont, 16, bold=True)

        probe = Image.new("RGB", (width, 100), "#f6f3ec")
        draw = ImageDraw.Draw(probe)

        summary_lines = self._wrap_text(draw, data["summary"], font_body, 720)
        title_lines = self._wrap_text(draw, data["title"], font_title, 720)
        header_h = max(
            150,
            34 + len(title_lines) * 52 + len(summary_lines) * 28,
        )

        prepared_rows = []
        for row in data["rows"]:
            left_lines = self._format_side_lines(draw, row["left_good"], row["left_bad"], font_body, side_w - 32)
            right_lines = self._format_side_lines(draw, row["right_good"], row["right_bad"], font_body, side_w - 32)
            aspect_lines = self._wrap_text(draw, row["aspect"], font_head, aspect_w - 28)
            row_h = max(
                98,
                len(left_lines) * 27 + 34,
                len(right_lines) * 27 + 34,
                len(aspect_lines) * 28 + 34,
            )
            prepared_rows.append((row, aspect_lines, left_lines, right_lines, row_h))

        table_h = 54 + sum(item[4] for item in prepared_rows)
        final_lines = self._wrap_text(draw, data["final"], font_h2, width - margin * 2 - 40)
        final_h = max(72, len(final_lines) * 34 + 34)
        height = margin + header_h + gap + table_h + gap + final_h + margin

        image = Image.new("RGB", (width, height), "#f6f3ec")
        draw = ImageDraw.Draw(image)

        self._draw_header(draw, data, title_lines, summary_lines, font_title, font_body, font_small, font_h2, width, margin, header_h)

        y = margin + header_h + gap
        self._rect(draw, [x0, y, x3, y + 54], "#1f2933", "#1f2933")
        self._text(draw, (x0 + 16, y + 15), "维度", font_head, "#ffffff")
        self._text(draw, (x1 + 16, y + 15), data["left"], font_head, "#ffffff")
        self._text(draw, (x2 + 16, y + 15), data["right"], font_head, "#ffffff")
        y += 54

        for index, (row, aspect_lines, left_lines, right_lines, row_h) in enumerate(prepared_rows):
            bg = "#fffdfa" if index % 2 == 0 else "#f8f5ef"
            self._rect(draw, [x0, y, x3, y + row_h], bg, "#1f2933")
            draw.line([(x1, y), (x1, y + row_h)], fill="#1f2933", width=2)
            draw.line([(x2, y), (x2, y + row_h)], fill="#1f2933", width=2)
            self._rect(draw, [x0, y, x1, y + row_h], "#e4edf4", "#1f2933")
            self._draw_lines(draw, aspect_lines, x0 + 14, y + 18, font_head, "#1f2933", 28)
            self._draw_side(draw, left_lines, x1 + 16, y + 16, font_body)
            self._draw_side(draw, right_lines, x2 + 16, y + 16, font_body)
            y += row_h

        final_y = y + gap
        self._rect(draw, [margin, final_y, width - margin, final_y + final_h], "#fffdfa", "#c65f3a", 3)
        draw.rectangle([margin, final_y, margin + 8, final_y + final_h], fill="#c65f3a")
        self._draw_lines(draw, final_lines, margin + 24, final_y + 18, font_h2, "#1f2933", 34)

        output = self.cache_dir / f"compare-{uuid.uuid4().hex}.png"
        image.save(output, "PNG")
        return output

    def _draw_header(
        self,
        draw: Any,
        data: dict[str, Any],
        title_lines: list[str],
        summary_lines: list[str],
        font_title: Any,
        font_body: Any,
        font_small: Any,
        font_h2: Any,
        width: int,
        margin: int,
        header_h: int,
    ) -> None:
        y = margin
        self._draw_lines(draw, title_lines, margin, y, font_title, "#1f2933", 52)
        self._draw_lines(draw, summary_lines, margin, y + len(title_lines) * 52 + 12, font_body, "#52616f", 28)
        box = [width - margin - 250, margin + 12, width - margin, margin + 112]
        self._rect(draw, box, "#cfe8dc", "#1f2933", 2)
        self._text(draw, (box[0] + 76, box[1] + 16), "综合判定", font_small, "#52616f")
        winner_lines = self._wrap_text(draw, data["winner"], font_h2, 210)
        self._draw_lines(draw, winner_lines[:2], box[0] + 20, box[1] + 45, font_h2, "#1f2933", 32)
        draw.line([(margin, margin + header_h - 1), (width - margin, margin + header_h - 1)], fill="#1f2933", width=3)

    def _format_side_lines(self, draw: Any, good: str, bad: str, font: Any, max_width: int) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        for tag, text in [("优", good), ("劣", bad)]:
            wrapped = self._wrap_text(draw, text, font, max_width - 54)
            for idx, line in enumerate(wrapped):
                lines.append((tag if idx == 0 else "", line))
        return lines

    def _draw_side(self, draw: Any, lines: list[tuple[str, str]], x: int, y: int, font: Any) -> None:
        for tag, text in lines:
            if tag:
                fill = "#d9f0df" if tag == "优" else "#ffe1d6"
                color = "#17613a" if tag == "优" else "#9b341f"
                self._rect(draw, [x, y + 2, x + 42, y + 25], fill, fill)
                self._text(draw, (x + 11, y + 2), tag, font, color)
            self._text(draw, (x + 54, y), text, font, "#1f2933")
            y += 27

    def _wrap_text(self, draw: Any, text: str, font: Any, max_width: int) -> list[str]:
        result: list[str] = []
        for raw_line in str(text).splitlines() or [""]:
            line = ""
            for char in raw_line:
                candidate = line + char
                if line and self._text_width(draw, candidate, font) > max_width:
                    result.append(line)
                    line = char
                else:
                    line = candidate
            result.append(line)
        return result or [""]

    def _draw_lines(
        self, draw: Any, lines: list[str], x: int, y: int, font: Any, fill: str, line_h: int
    ) -> None:
        for line in lines:
            self._text(draw, (x, y), line, font, fill)
            y += line_h

    def _text(self, draw: Any, xy: tuple[int, int], text: str, font: Any, fill: str) -> None:
        draw.text(xy, str(text), font=font, fill=fill)

    def _text_width(self, draw: Any, text: str, font: Any) -> int:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    def _rect(self, draw: Any, xy: list[int], fill: str, outline: str, width: int = 2) -> None:
        draw.rectangle(xy, fill=fill, outline=outline, width=width)

    def _load_font(self, image_font: Any, size: int, bold: bool = False) -> Any:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ]
        for path in candidates:
            try:
                return image_font.truetype(path, size=size)
            except Exception:
                continue
        return image_font.load_default()

    def _cleanup_image_cache(self) -> None:
        deadline = time.time() - 24 * 60 * 60
        for path in self.cache_dir.glob("compare-*.png"):
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                continue

    async def terminate(self):
        pass
