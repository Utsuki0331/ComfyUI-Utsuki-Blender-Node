import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "Utsuki.BlenderUI",
    
    nodeCreated(node) {
        // 同时匹配 AnimationRender 和 FrameRender
        if (node.comfyClass.includes("UtsukiBlender") && node.comfyClass.includes("Render")) {
            if (node.widgets.find(w => w.name === "select_blend_btn")) return;

            // 1. 浏览文件按钮
            const blendPathIndex = node.widgets.findIndex(w => w.name === "blend_path");
            if (blendPathIndex !== -1) {
                node.widgets.splice(blendPathIndex + 1, 0, {
                    type: "button", name: "select_blend_btn", value: "",
                    label: "📁 浏览本地 Blender 文件 (可多选)",
                    callback: async () => {
                        try {
                            const resp = await fetch("/utsuki/select_blend_files");
                            const data = await resp.json();
                            if (data.paths && data.paths.length > 0) {
                                node.widgets[blendPathIndex].value = data.paths[0];
                                if (node.widgets[blendPathIndex].callback) node.widgets[blendPathIndex].callback(data.paths[0]);
                                let startX = node.pos[0] - 320, startY = node.pos[1];
                                data.paths.forEach((path, i) => {
                                    const newFileNode = LiteGraph.createNode("UtsukiBlendFile");
                                    if (newFileNode) {
                                        newFileNode.pos = [startX, startY + i * 120];
                                        const pathWidget = newFileNode.widgets.find(w => w.name === "blend_path");
                                        if (pathWidget) pathWidget.value = path;
                                        app.graph.add(newFileNode);
                                    }
                                });
                                app.graph.setDirtyCanvas(true, true);
                            }
                        } catch (err) { console.error(err); }
                    }
                });
            }

            // 2. 刷新相机按钮
            const camIndex = node.widgets.findIndex(w => w.name === "camera_name");
            if (camIndex !== -1) {
                node.widgets.splice(camIndex + 1, 0, {
                    type: "button", name: "refresh_cam_btn", value: "",
                    label: "🔄 刷新相机列表",
                    callback: async () => {
                        const bp = node.widgets.find(w => w.name === "blend_path").value;
                        if (!bp) return alert("请先指定 .blend 文件");
                        const btn = node.widgets.find(w => w.name === "refresh_cam_btn");
                        btn.label = "⏳ 扫描中..."; app.graph.setDirtyCanvas(true, true);
                        
                        try {
                            const resp = await fetch(`/utsuki/get_cameras?blend_path=${encodeURIComponent(bp)}`);
                            const data = await resp.json();
                            if (data.cameras && data.cameras.length > 0) {
                                const sel = prompt("检测到以下相机，请选择或输入：\n\n" + data.cameras.join("\n"));
                                if (sel) {
                                    const camW = node.widgets.find(w => w.name === "camera_name");
                                    camW.value = sel;
                                    if (camW.callback) camW.callback(sel);
                                }
                            } else {
                                alert("未检测到相机，或解析失败。");
                            }
                        } catch(e) { console.error(e); } finally {
                            btn.label = "🔄 刷新相机列表"; app.graph.setDirtyCanvas(true, true);
                        }
                    }
                });
            }

            // 3. 构建原生 DOM 进度条
            const progressBarContainer = document.createElement("div");
            progressBarContainer.style.cssText = "width:100%; margin-top:8px; padding:4px; box-sizing:border-box; background:#222; border-radius:4px; color:#fff; font-size:12px; font-family:sans-serif; display:none;";
            
            const progressText = document.createElement("div");
            progressText.innerText = "等待渲染...";
            progressText.style.marginBottom = "4px";

            const progressTrack = document.createElement("div");
            progressTrack.style.cssText = "width:100%; height:8px; background:#444; border-radius:4px; overflow:hidden;";
            const progressFill = document.createElement("div");
            progressFill.style.cssText = "width:0%; height:100%; background:#2196F3; transition:width 0.3s;";
            
            progressTrack.appendChild(progressFill);
            progressBarContainer.appendChild(progressText);
            progressBarContainer.appendChild(progressTrack);

            node.addDOMWidget("progress_ui", "div", progressBarContainer, {
                serialize: false, hideOnZoom: false
            });

            node.utsukiProgress = { container: progressBarContainer, text: progressText, fill: progressFill };

            // 4. 折叠日志
            const details = document.createElement("details");
            details.style.cssText = "width:100%; padding:4px; box-sizing:border-box; background:#2a2a2a; border-radius:4px; margin-top:8px;";
            const summary = document.createElement("summary");
            summary.innerText = "查看超长渲染日志";
            summary.style.cssText = "cursor:pointer; color:#ccc; font-family:sans-serif; font-size:12px; outline:none;";
            const textArea = document.createElement("textarea");
            textArea.readOnly = true;
            textArea.style.cssText = "width:100%; height:150px; margin-top:8px; background:#111; color:#0f0; font-family:monospace; font-size:11px; border:1px solid #444; resize:vertical; padding:4px;";
            textArea.placeholder = "渲染日志将在这里显示...";
            details.appendChild(summary);
            details.appendChild(textArea);
            
            node.addDOMWidget("render_log_ui", "textarea", details, {
                serialize: false, hideOnZoom: false
            });

            const onExecuted = node.onExecuted;
            node.onExecuted = function(message) {
                if (onExecuted) onExecuted.apply(this, arguments);
                if (message && message.text && message.text.length > 0) {
                    textArea.value = message.text[0];
                }
                if (node.utsukiProgress) {
                    node.utsukiProgress.container.style.display = "none";
                }
                app.graph.setDirtyCanvas(true, true);
            };
            
            const size = node.computeSize();
            node.setSize([Math.max(node.size[0], size[0], 350), size[1]]);
        }
    }
});

// 全局底层 WebSocket 拦截器
app.registerExtension({
    name: "Utsuki.ProgressListener",
    async setup() {
        const ws = app.api?.socket || app.api;
        if (!ws || !ws.addEventListener) return;
        
        ws.addEventListener("message", (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === "utsuki_progress") {
                    const data = msg.data;
                    const node = app.graph.getNodeById(Number(data.node_id));
                    
                    if (node && node.utsukiProgress) {
                        const ui = node.utsukiProgress;
                        ui.container.style.display = "block";
                        
                        let pct = Math.min(100, (data.relative_frame / data.total) * 100);
                        ui.fill.style.width = `${pct}%`;
                        
                        ui.text.innerText = `进度: ${data.relative_frame}/${data.total} | 当前帧: ${data.current_frame} | 耗时: ${data.elapsed} | 速度: ${data.fps} | 剩余: ${data.eta}`;
                        
                        app.graph.setDirtyCanvas(true, true);
                    }
                }
            } catch(e) {}
        });
    }
});