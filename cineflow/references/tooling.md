# 工具与执行

以下命令在仓库根目录执行；安装 skill 后，相应替换脚本路径。Python 脚本仅用标准库，后期需要本机的 ffmpeg / ffprobe。

## 环境与模型

在本地 shell 或 Agent 环境配置 `ARK_API_KEY`、`SEEDANCE_MODEL`。可选 `ARK_BASE_URL` 默认是 `https://ark.cn-beijing.volces.com/api/v3`。脚本不自动加载 `.env`，不应把真实凭据写入提示词、项目记录或仓库。

每次选模型前核对 [创建视频任务文档](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) 和账号权限、计价、时长、分辨率、音轨和水印参数。

脚本支持文字、单首帧、首尾帧三种输入；未实现全模态参考输入。官方的单首帧、首尾帧和全模态参考是互斥模式，不可随意将人物参考图和首尾帧混发。角色设定可用于前期生成首尾帧。

生图使用宿主实际提供的工具；如果通过另一 Agent 调用，按其当前官方用法配置，不依赖某条旧版命令或固定输出路径。

## 生成一个镜头

先在独立项目目录保存提示词和已审核的图片，例如 `projects/demo/shot01.txt`、`first.png`、`last.png`。父目录需已存在。

```bash
python3 cineflow/scripts/seedance.py create \
  --state projects/demo/shot01-v1.state.json \
  --prompt-file projects/demo/shot01.txt \
  --model "$SEEDANCE_MODEL" \
  --duration 5 --ratio 9:16 --resolution 720p \
  --first-frame projects/demo/first.png \
  --last-frame projects/demo/last.png
```

默认只做本地预览，不提交、不需要 API Key。预览输出参数摘要及请求指纹，不输出图片数据。

**文字直接生视频**：省去两个 frame 参数。**单首帧**：只传 `--first-frame`。`--last-frame` 不能单独使用。是否使用首尾帧按项目流程与效果目标决定。

取得费用授权后，在已确认的同一命令追加 `--submit` 才会提交。脚本默认无音轨、有水印；需要模型音轨时加 `--generate-audio`。其他可选参数见 `create --help`。

可同时设置 `--estimated-cost 10 --budget 15`，做同币种、单请求的本地估算检查。这不是实际账单或累计预算上限；Agent 仍须逐笔记录并核对总预算。此处金额仅为参数演示。

## 查询、下载和中断恢复

```bash
python3 cineflow/scripts/seedance.py poll \
  --state projects/demo/shot01-v1.state.json --wait-timeout 45
python3 cineflow/scripts/seedance.py download \
  --state projects/demo/shot01-v1.state.json \
  --output projects/demo/shot01-v1.mp4
```

`get` 查询一次；`poll` 持续查询，超时后用原 state 再查，不另建任务。Agent 采用短时轮询并在间隙汇报进度，避免长时间不回应。

state 在提交前创建以阻止并发重复；已有任务时同一 create 命令加 `--resume` 仅返回编号，不重新提交。新镜头、新输入或重拍须使用新版本 state。

如果提交超时或返回损坏，state 保留为结果不明。先去方舟任务记录核实，不删除 state 后直接重试。查到对应编号后恢复：

```bash
python3 cineflow/scripts/seedance.py get \
  --state projects/demo/shot01-v1.state.json \
  --task-id TASK_ID_FROM_ARK
```

若平台明确确认未创建任务，记录核查结果，再用新版本 state 重新提交。任务失败、取消或过期时同样先核对原因及费用，修改输入并按授权处理。

state 含结果下载地址，作为项目文件本地保存，不公开。下载不向结果 URL 发送 API Key，已有目标文件不会被覆盖。

## 探测、抽帧和拼接

```bash
python3 cineflow/scripts/video_tools.py probe projects/demo/shot01-v1.mp4
python3 cineflow/scripts/video_tools.py contact-sheet \
  projects/demo/shot01-v1.mp4 -o projects/demo/sheet-v1.jpg --interval 2
python3 cineflow/scripts/video_tools.py contact-sheet \
  projects/demo/rough-v1.mp4 -o projects/demo/cuts-v1.png --times 0,4.5,9
python3 cineflow/scripts/video_tools.py concat \
  projects/demo/shot01-v1.mp4 projects/demo/shot02-v1.mp4 \
  -o projects/demo/rough-v1.mp4
```

指定二进制可用全局 `--ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe`（放在子命令之前），或 `FFMPEG_BIN` / `FFPROBE_BIN`。

抽帧总览用于发现静态问题，输出记录每格请求采样时间。切点附近可根据实际帧率传入更密集的时间。

`concat` 只拼接兼容的流，拒绝明显不一致的编码、尺寸、帧率、音轨等。它不调色、不统一编码、不加转场。需要复杂后期时，由 Agent 编写适合项目的 ffmpeg 处理命令，先检查实际媒体属性，不强行指定所有输入均为 BT.601。输出须使用新版本文件名。
