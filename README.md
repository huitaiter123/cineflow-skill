# 一念成片 · CineFlow

### 把一段文字，变成一支完整视频。

你负责想象，AI 负责把它拍出来。

写下一个脑洞、一段故事，或一个产品卖点。CineFlow 把创意拆成故事、分镜、画面和视频，再组织剪辑、配乐与字幕，让你的文字一步步变成可以播放的作品。

**一个面向 Codex、Claude Code 等 Agent 的文生视频 Skill。**

[开始使用](#开始使用) · [制作流程](#从文字到视频) · [工具与配置](#需要准备什么) · [灵感来源](#致谢与来源)

## 你说想拍什么，AI 接手制作流程

> “做一条 30 秒竖屏短片：一个总在加班的机器人，第一次学会准时下班。温暖的二维动画，结尾让人会心一笑。”

从这样的文字出发，CineFlow 会组织完成：

- **写出故事**：把零散想法变成观众能看懂、愿意看下去的剧情。
- **设计镜头**：安排每一镜的构图、动作、时长、配色与转场。
- **生成画面**：先定画风和角色，再制作每个镜头的首尾帧。
- **生成视频**：通过 Seedance 将静态画面转为视频片段。
- **完成后期**：组织剪辑、配乐、字幕与导出，交付视频及项目材料。

你不需要先学会写专业提示词。有参考图或素材就带上；只有文字，也可以从故事开始。

## 为什么要让 AI 当导演

一个想法变成好看的视频，中间还有很多决定：开头怎么抓人、镜头怎么切、人物为什么行动、画面如何保持一致。

CineFlow 把这些工作串进同一条制作流程。先确认故事和画风，再逐镜生成；把能提前发现的问题留在文字和图片阶段解决，减少进入视频生成后的返工。

**你保留关键选择，Agent 负责推进制作。**

## 从文字到视频

```mermaid
flowchart LR
    A[文字创意] --> B[故事与审核]
    B --> C[分镜与配色]
    C --> D[画风与角色]
    D --> E[首尾帧]
    E --> F[镜头审核与报价]
    F --> G[视频生成]
    G --> H[剪辑与声音]
    H --> I[复核与交付]
```

三个关键确认点：**故事确认、画风确认、费用授权**。如果你已经给出明确的预算与执行范围，Agent 可以在该范围内继续制作。

支持按需安排主做、审核、监督与代理角色。审核者检查作品，监督者核对需求；代理只能在你授权的范围内做决定。

## 开始使用

### 1. 安装 Skill

下载本仓库，或执行：

```bash
git clone https://github.com/huitaiter123/cineflow-skill.git
cd cineflow-skill
```

复制 `cineflow` 文件夹到所用 Agent 的技能目录。Codex 个人安装示例：

```bash
mkdir -p ~/.codex/skills
cp -R cineflow ~/.codex/skills/
```

Claude Code 可放到 `~/.claude/skills/cineflow/`，也可以在项目中使用 `.claude/skills/cineflow/`。不同宿主的生图、子代理和工具权限按其实际配置使用。

### 2. 说出你的创意

在支持此 Skill 的 Codex 会话中：

```text
使用 $cineflow 做一条文生视频。
创意：一个总在加班的机器人，第一次学会准时下班。
片长：30 秒，9:16。
画风：温暖的二维动画。
预算：先给我方案和报价，我确认后再生成。
先帮我把故事写好。
```

Claude Code 可使用 `/cineflow`，或直接要求 Agent 读取本技能。完整输入模板见 [创作需求表](cineflow/assets/brief-template.md)。

### 3. 在关键节点做选择

看故事、选画风、确认生成方案。Agent 将记录镜头、审核结果、任务编号和版本；你可以随时调整方向。

## 适合拿来拍什么

- 脑洞故事、角色短片、动画小剧场。
- 产品卖点、品牌创意、社交媒体宣传片。
- 有清楚主题、视觉风格和节奏要求的短视频。

具体可生成的内容、时长和画幅取决于所选视频服务与模型能力。

## 需要准备什么

- **一个可以调用工具的 Agent**：负责策划、编排和文件操作。
- **可用的生图能力**：负责样帧、角色与首尾帧。
- **火山方舟账号与已开通的 Seedance 模型**：负责视频生成。
- **Python 3.10+**；后期工具需要 **ffmpeg / ffprobe**。
- 配乐、旁白、品牌素材按项目需要提供；AI 配音可另接语音服务。

账号凭据配置在本地环境中。仓库提供 [.env.example](.env.example)，脚本直接读取环境变量，**不会自动加载 `.env` 文件**。

Skill 负责组织制作，生成图片和视频使用你自己的工具、账号与额度。模型、价格和可用参数按执行时的官方文档及账号核对。

## 附带的执行工具

- [Seedance 工具](cineflow/scripts/seedance.py)：预览请求、提交任务、查询进度、下载视频；保存任务编号，支持继续查询已有任务。
- [视频工具](cineflow/scripts/video_tools.py)：探测视频信息、生成抽帧总览、拼接兼容的视频片段。
- [制作规范](cineflow/references/director.md)：故事、镜头、首尾帧、提示词与审核要点。
- [工具使用说明](cineflow/references/tooling.md)：配置、命令示例与恢复方式。

Seedance 创建命令默认只预览，显式使用 `--submit` 才会提交。执行前由 Agent 按你的授权核对费用与范围。

后期脚本提供基础工具；调色、转场、声音和字幕由 Agent 按项目编排。720p 放大到 1080p 属于放大交付；最终动态、节奏与声音需要实际播放确认。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
python3 cineflow/scripts/seedance.py --help
python3 cineflow/scripts/video_tools.py --help
```

自动化检查不需要视频服务凭据，不提交付费任务。

## 致谢与来源

制作方法参考 Eian 的 [《Claude code/Codex+Seedance AI视频：从一个想法到成片的全流程》](https://x.com/EianLu/status/2118317430665613741)。本项目将其流程思路重新整理为 Agent 指令、模板和辅助脚本，未收录原文全文、配图或整套提示词。

接口信息以 [火山方舟文档](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) 为准；Skill 结构参考 [OpenAI 文档](https://developers.openai.com/plugins/build/skills)。本项目与上述作者和平台无隶属关系。

代码与本项目原创文档采用 [MIT License](LICENSE)。第三方素材遵循各自的授权。
