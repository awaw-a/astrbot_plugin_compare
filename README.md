# astrbot-plugin-compare

一个 AstrBot 群聊对比插件。群友问“两样东西谁更厉害”时，插件会调用当前会话配置的 LLM，分维度列出两者优劣，最后在本地生成 PNG 图片发送。

## 功能

- 支持 `/compare`、`/比较`、`/对比`、`/谁强`。
- 自动调用当前会话使用的聊天模型。
- 使用 Pillow 在本地生成 PNG 图片，不依赖 AstrBot 的 t2i 网络渲染端点。
- 输出包含多个维度、双方优劣、综合判定和最终结论的图片。

## 用法

```text
/compare Python vs Java
/比较 可口可乐 和 百事可乐
/对比 iPhone 17 与 Android 旗舰
```

## 安装

1. 把本文件夹放到 AstrBot 的 `data/plugins/` 目录。
2. 确认插件目录里有 `requirements.txt`，内容包含 `Pillow>=10.0.0`。
3. 重启 AstrBot，或在 WebUI 的插件管理里重新加载插件，让 AstrBot 安装依赖。
4. 如果提示缺少 Pillow，请在 AstrBot WebUI 的 `控制台` -> `安装 Pip 包` 中安装 `Pillow`。
5. 确保 AstrBot 已经配置可用的 LLM 提供商。
6. 在群聊或私聊里发送上面的命令测试。

## 开发说明

- 插件入口：`main.py`
- LLM 调用：`Context.llm_generate`
- 图片生成：`Pillow`
- 图片发送：`event.image_result(本地 PNG 路径)`
