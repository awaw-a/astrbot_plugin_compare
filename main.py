import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("compare", "aw4w", "调用 LLM 生成两者优劣对照表并输出图片", "1.0.0")
class ComparePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.template_path = Path(__file__).parent / "templates" / "compare.html"

    @filter.command("compare", alias={"比较", "对比", "谁强"})
    async def compare(self, event: AstrMessageEvent):
        """比较两个对象，并以图片表格输出结果。"""
        try:
            left, right = self._parse_items(event.message_str)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        yield event.plain_result(f"正在比较「{left}」和「{right}」，我先问问模型。")

        try:
            data = await self._generate_compare_data(event, left, right)
            template = self.template_path.read_text(encoding="utf-8")
            image_url = await self.html_render(
                template,
                data,
                options={
                    "type": "png",
                    "full_page": True,
                    "timeout": 30,
                    "animations": "disabled",
                },
            )
        except Exception as exc:
            logger.exception("HTML 对比图渲染失败，尝试使用纯文本图片兜底")
            try:
                fallback_text = self._build_fallback_text(data)
                image_url = await self.text_to_image(fallback_text)
            except Exception:
                logger.exception("纯文本图片兜底也失败")
                yield event.plain_result(f"生成对比失败：{exc}")
                return

        yield event.image_result(image_url)

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

    def _build_fallback_text(self, data: dict[str, Any]) -> str:
        lines = [
            data["title"],
            f"综合判定：{data['winner']}",
            data["summary"],
            "",
        ]
        for row in data["rows"]:
            lines.extend(
                [
                    f"【{row['aspect']}】",
                    f"{data['left']} 优：{row['left_good']}",
                    f"{data['left']} 劣：{row['left_bad']}",
                    f"{data['right']} 优：{row['right_good']}",
                    f"{data['right']} 劣：{row['right_bad']}",
                    "",
                ]
            )
        lines.append(data["final"])
        return "\n".join(lines)

    async def terminate(self):
        pass
