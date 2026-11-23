import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "Utsuki.BlenderUI",
    
    nodeCreated(node) {
        if (node.comfyClass.includes("UtsukiBlender") && node.comfyClass.includes("Render")) {
            if (node.widgets.find(w => w.name === "select_blend_btn")) return;

            // 0. 注入持久化 UUID
            const persistWidget = node.widgets.find(w => w.name === "persist_id");
            if (persistWidget) {
                if (!persistWidget.value) {
                    persistWidget.value = Math.random().toString(36).substring(2, 10) + Math.random().toString(36).substring(2, 10);
                }
                persistWidget.computeSize = () => [0, -4]; 
                persistWidget.draw = () => {};
            }

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
                                const choice = prompt(
                                    "请选择要生成的节点类型（输入数字 1/2/3）：\n\n" +
                                    "1 - 生成 Blend File 节点\n" +
                                    "2 - 生成 Animation Render 节点\n" +
                                    "3 - 生成 Frame Render 节点",
                                    "2"
                                );
                                if (!["1", "2", "3"].includes(choice)) return;

                                const nodeTypeMap = {
                                    "1": "UtsukiBlendFile",
                                    "2": "UtsukiBlenderAnimationRender",
                                    "3": "UtsukiBlenderFrameRender"
                                };
                                const targetClass = nodeTypeMap[choice];

                                node.widgets[blendPathIndex].value = data.paths[0];
                                if (node.widgets[blendPathIndex].callback) {
                                    node.widgets[blendPathIndex].callback(data.paths[0]);
                                }

                                let startX = node.pos[0] - 380;
                                let startY = node.pos[1];

                                data.paths.forEach((path, i) => {
                                    const newNode = LiteGraph.createNode(targetClass);
                                    if (newNode) {
                                        const yOffset = (targetClass === "UtsukiBlendFile") ? 120 : 550;
                                        newNode.pos = [startX, startY + i * yOffset];
                                        
                                        const pathWidget = newNode.widgets.find(w => w.name === "blend_path");
                                        if (pathWidget) pathWidget.value = path;
                                        
                                        app.graph.add(newNode);
                                    }
                                });
                                app.graph.setDirtyCanvas(true, true);
                            }
                        } catch (err) { console.error(err); }
                    }
                });
            }

            // 2. 构建 DOM 进度条 (默认隐藏)
            const progressBarContainer = document.createElement("div");
            progressBarContainer.style.cssText = "width:100%; margin-top:8px; padding:4px; box-sizing:border-box; background:#222; border-radius:4px; color:#fff; font-size:12px; font-family:sans-serif; display:none;";
            
            const progressText = document.createElement("div");
            progressText.innerText = "准备渲染...";
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

            // 3. 折叠日志
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
                    const nodeId = Number(data.node_id);
                    const node = app.graph.getNodeById(nodeId);
                    
                    if (node && node.utsukiProgress) {
                        const ui = node.utsukiProgress;
                        ui.container.style.display = "block"; 
                        
                        let pct = (data.current / data.total) * 100;
                        if (isNaN(pct) || pct < 0) pct = 0;
                        if (pct > 100) pct = 100;
                        
                        ui.fill.style.width = `${pct}%`;
                        
                        const formatTime = (sec) => {
                            if (sec < 0) sec = 0;
                            if (sec < 60) return `${Math.floor(sec)}s`;
                            const m = Math.floor(sec / 60);
                            const s = Math.floor(sec % 60);
                            return `${m}m${s}s`;
                        };

                        const avgStr = data.avg_time > 0 ? formatTime(data.avg_time) : "-";
                        const etaStr = data.eta > 0 ? formatTime(data.eta) : "-";

                        ui.text.innerText = `进度: ${data.current}/${data.total} | 帧: ${data.frame} | 耗时: ${formatTime(data.elapsed)} | 单帧: ${avgStr} | 剩余: ${etaStr}`;
                        
                        app.graph.setDirtyCanvas(true, true);
                    }
                }
            } catch(e) {}
        });
    }
});