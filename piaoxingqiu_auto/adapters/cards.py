"""飞书消息卡片构建。"""

from __future__ import annotations

import time


def build_card(title: str, body_md: str, template: str = "red") -> dict:
    """构建飞书应用机器人消息卡片主体。"""
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                ],
            },
        ],
    }
