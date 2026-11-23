🦊 Utsuki Blender Node for ComfyUI
这是一个专为 ComfyUI 打造的 Blender 渲染桥接插件。它允许你在 ComfyUI 的工作流中直接调用本地安装的 Blender 软件，进行动画渲染或单帧序列渲染。插件内置了强大的进程防崩溃隔离、断点续传、实时进度条、超时强杀以及飞书通知功能。

✨ 核心特性
🎬 多模式渲染：支持 Animation Render (连续动画帧) 与 Frame Render (指定单帧序列)。

🛡️ 防崩溃与断点续传：每次渲染完一帧都会将进度写入 JSON 缓存。若 Blender 意外闪退，可选择继续渲染或自动重试，绝不浪费已渲染的帧。

🚀 零阻塞异步运行：使用子进程隔离运行 Blender，渲染期间 ComfyUI 界面绝不卡顿，且支持随时取消（点 ComfyUI 的取消即可强杀 Blender）。

📊 原生级进度条：节点下方内嵌进度条，实时显示当前帧、总帧数、单帧平均耗时、总耗时与预估剩余时间。

📩 飞书机器人通知：渲染成功、崩溃重试、彻底失败或超时，均可推送到你的飞书群。


📁 一键多选批量生成：点击浏览按钮可多选 .blend 文件，自动在画布上铺开生成对应的渲染节点。

📦 安装方法
确保你已经安装了 ComfyUI 和一个可用的 Blender 客户端。

进入 ComfyUI 的 custom_nodes 目录：

bash
cd ComfyUI/custom_nodes
克隆本仓库：

bash
git clone https://github.com/Utsuki0331/ComfyUI-Utsuki-Blender-Node.git
重启 ComfyUI。启动时请确保带有 --allow-subprocess 启动参数（如果在某些安全限制较严格的启动器中运行，请务必加上此项）。

🧩 节点详解
1. Blend File (UtsukiBlendFile)
轻量级的文件路径管理节点。
<img width="973" height="383" alt="image" src="https://github.com/user-attachments/assets/56f0a2dd-bc20-4d00-80e0-67a90d64cce4" />

输入：blend_path (Blender 文件的绝对路径)。

输出：blend_path (字符串)，可以直接连接到渲染节点的对应输入口。

2. Blender Animation Render
用于渲染连续动画范围（例如 1 到 100 帧）。
<img width="1061" height="1702" alt="image" src="https://github.com/user-attachments/assets/f24a8c95-e0ea-46b0-91b5-e95259614ac6" />

🎛️ 输入端参数详解
参数名	类型	说明
any_input	任意	触发线，连接上一个节点的输出即可唤醒执行。
blend_path	字符串	.blend 文件路径。可手动输入或连线上游节点。
render_mode	下拉框	重启：无视缓存，从头开始渲染。
继续：读取缓存，从上次中断的那一帧继续。
render_engine	下拉框	强制覆盖引擎：默认 (不修改原文件)、CYCLES、EEVEE、WORKBENCH。
start_frame	整数	起始帧。填 -1 代表使用 .blend 文件自带的起始帧。
end_frame	整数	结束帧。填 -1 代表使用 .blend 文件自带的结束帧。
step	整数	渲染步长（每隔多少帧渲染一次）。默认 1。
res_x / res_y	整数	分辨率覆盖。填 -1 代表使用文件自带的分辨率。
res_percent	整数	分辨率百分比覆盖（例如 50 代表半分辨率）。填 -1 代表原设置。
timeout_minutes	整数	超时强杀。如果渲染卡死（超过多少分钟没有输出新帧），将强制终止 Blender 进程。0 代表不限制。
max_retries	整数	崩溃重试。渲染崩溃后的自动重试次数，会精准从断点续传。0 代表不重试。
feishu_url	字符串	飞书机器人的 Webhook 地址（留空则不发送通知）。
feishu_secret	字符串	飞书机器人的签名校验密钥（若开启了安全设置则必填）。
custom_params	多行文本	注入自定义 Python 代码（见下方进阶指南）。
📤 输出端参数
node_log：节点自身的摘要日志（成功/失败提示、输出路径等）。

blender_log：Blender 终端的完整控制台原始日志。

image：渲染完成后的第一张图片预览，可连入 Preview Image 节点。

3. Blender Frame Render
用于渲染指定的离散单帧（例如 1, 5, 10 帧）。
<img width="1070" height="1658" alt="image" src="https://github.com/user-attachments/assets/1eaeb90b-c47f-468e-8cd0-faf2f6b26e99" />

与 Animation 节点的唯一区别：移除了起止帧，新增了 frames_input 多行文本框。

frames_input：输入需要渲染的帧号，支持英文逗号、中文逗号、空格和换行分隔（如 1, 5, 10）。渲染时会自动将 ### 替换为真实帧号，不会覆盖文件。

⚙️ 进阶指南
🚀 飞书通知配置
在飞书群中添加“自定义机器人”。

复制 Webhook 地址填入 feishu_url。

如果开启了“签名校验”，将密钥填入 feishu_secret。

节点会在成功 (绿色)、重试中 (橙色)、彻底失败 (红色)、超时 (红色) 时推送精美的消息卡片。

📝 自定义参数注入 (custom_params)
你可以直接在框内输入 Python 代码，它会在 Blender 渲染前执行。例如，修改渲染的比例：

python
bpy.data.scenes["Scene"].render.pixel_aspect_x = 2
注意：此操作仅在内置内存中修改，绝不会破坏原始的 .blend 文件。

💾 断点续传机制
每次渲染完一帧，节点会自动在 ComfyUI 的 temp/utsuki_blender_cache/ 下生成一个与节点绑定的 UUID.json 文件。

若意外断电或手动关闭了 ComfyUI，下次打开工作流时，将 render_mode 切换为 “继续 (Resume)”，节点会根据 JSON 里的记录，完美跳过已渲染的帧，从断点继续。

如果想从头重跑，只需切回 “重启 (Restart)”，缓存会被自动清理。

❓ 常见问题 (FAQ)
Q: 为什么渲染时无法取消，或取消后 Blender 还在后台跑？
A: 插件已经写入了 ComfyUI 的中断拦截信号。正常情况下点击“取消”会瞬间强杀 Blender。若偶尔遇到 Win 系统的僵尸进程，你可以按 Ctrl+Shift+Esc 打开任务管理器，手动结束 blender.exe。

Q: 为什么找不到我安装的 Blender？
A: 插件会自动读取系统注册表（Windows）查找默认关联的 .blend 文件打开程序。请先在系统中双击任意 .blend 文件，并勾选“始终使用此应用打开”。如果仍然不行，请把 Blender 加入系统环境变量 Path。

Q: 渲染完成但图片输出不对？
A: 请确保你在 .blend 文件中的输出路径（Output Path）设置正确。如果包含相对路径（//），它是相对于 .blend 文件目录的。

