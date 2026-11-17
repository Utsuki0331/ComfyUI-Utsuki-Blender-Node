import os
import sys
import re
import json
import time
import shlex
import shutil
import subprocess
import tempfile
import glob
import torch
import numpy as np
import threading
from PIL import Image
from server import PromptServer

# ============================================================
# 跨平台：查询 .blend 文件的默认打开程序
# ============================================================
def _get_default_windows(ext=".blend"):
    import winreg
    try:
        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        cmd_path = rf"SOFTWARE\Classes\{prog_id}\shell\open\command"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cmd_path) as key:
            command = winreg.QueryValueEx(key, "")[0]
        return shlex.split(command)[0]
    except Exception:
        pass
    try:
        class_root = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, ext)
        cmd_path = rf"{class_root}\shell\open\command"
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
            command = winreg.QueryValueEx(key, "")[0]
        return shlex.split(command)[0]
    except Exception:
        return None

def _get_default_macos(ext="blend"):
    candidates = ["/Applications/Blender.app", os.path.expanduser("~/Applications/Blender.app")]
    for app in candidates:
        exe = os.path.join(app, "Contents/MacOS/Blender")
        if os.path.isfile(exe): return exe
    try:
        result = subprocess.run(["mdfind", "kMDItemFSName == 'Blender.app'"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.strip().splitlines():
            exe = os.path.join(line.strip(), "Contents/MacOS/Blender")
            if os.path.isfile(exe): return exe
    except Exception:
        pass
    return None

def _get_default_linux(ext=".blend"):
    for mime in ["application/x-blender", "application/blender"]:
        try:
            result = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=10)
            if result.stdout.strip():
                return _parse_desktop_exec(result.stdout.strip())
        except Exception:
            pass
    return None

def _parse_desktop_exec(desktop_name):
    for d in [os.path.expanduser("~/.local/share/applications"), "/usr/share/applications"]:
        path = os.path.join(d, desktop_name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("Exec="):
                            parts = shlex.split(line.strip()[5:].strip())
                            if parts: return parts[0]
            except Exception:
                pass
    return None

def find_default_blender():
    if sys.platform.startswith("win"): exe = _get_default_windows(".blend")
    elif sys.platform == "darwin": exe = _get_default_macos("blend")
    else: exe = _get_default_linux(".blend")

    if exe and os.path.basename(exe).lower() == "blender-launcher.exe":
        real_exe = os.path.join(os.path.dirname(exe), "blender.exe")
        if os.path.isfile(real_exe): return real_exe
    return exe

# ============================================================
# 子进程注入脚本 (Runner) - 兼容动画和单帧列表，修复路径叠加
# ============================================================
RUNNER_SOURCE = '''
import bpy
import os
import sys
import json
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

job_path = sys.argv[-1]
progress_path = job_path + ".progress"

try:
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    scene = bpy.context.scene

    if job.get("engine"): scene.render.engine = job["engine"]
    if job.get("res_x", -1) != -1: scene.render.resolution_x = job["res_x"]
    if job.get("res_y", -1) != -1: scene.render.resolution_y = job["res_y"]
    if job.get("res_percent", -1) != -1: scene.render.resolution_percentage = int(job["res_percent"])

    if job.get("camera_name"):
        obj = bpy.data.objects.get(job["camera_name"])
        if obj and obj.type == 'CAMERA': scene.camera = obj

    if job.get("custom_params", "").strip():
        try: exec(job["custom_params"], {"bpy": bpy})
        except Exception as e:
            print(f"UTSUKI_CUSTOM_ERROR={str(e)}")
            sys.exit(1)

    out_dir = bpy.path.abspath(scene.render.filepath)
    print(f"UTSUKI_OUT_DIR={out_dir}")

    frames_list = job.get("frames_list")

    def write_progress(scene, *args):
        try:
            with open(progress_path, "w") as f:
                f.write(f"{scene.frame_current}")
        except:
            pass

    bpy.app.handlers.render_post.append(write_progress)
    print("UTSUKI_START_RENDER")

    if frames_list:
        # 单帧列表渲染模式
        print(f"UTSUKI_TOTAL_FRAMES={len(frames_list)}")
        
        # 保存用户原本设置的带 ### 的基础路径
        original_filepath = scene.render.filepath
        
        for f in frames_list:
            scene.frame_set(int(f))
            
            # 关键修复：每次循环前，必须先还原到原始路径，再解析，防止路径无限叠加
            scene.render.filepath = original_filepath
            
            try:
                resolved_path = scene.render.frame_path(frame=int(f))
                scene.render.filepath = resolved_path
            except Exception:
                pass
                
            write_progress(scene)
            bpy.ops.render.render(write_still=True)
            
        # 渲染结束，把路径还原
        scene.render.filepath = original_filepath
    else:
        # 动画渲染模式
        if job.get("start_frame", -1) != -1: scene.frame_start = job["start_frame"]
        if job.get("end_frame", -1) != -1: scene.frame_end = job["end_frame"]
        if job.get("step", -1) != -1: scene.frame_step = job["step"]
        
        print(f"UTSUKI_RANGE={scene.frame_start},{scene.frame_end},{scene.frame_step}")
        write_progress(scene)
        bpy.ops.render.render(animation=True)

    print("UTSUKI_FINISH_RENDER")

except Exception as e:
    print(f"UTSUKI_PYTHON_ERROR={str(e)}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
'''

# ============================================================
# 公共逻辑抽取（用于底层执行、监控进度、断点续传）
# ============================================================
def execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id):
    blender = find_default_blender() or shutil.which("blender") or shutil.which("blender.exe")
    if not blender: return ("[Error] 未能定位 Blender。", "", torch.zeros((1, 64, 64, 3)))

    node_id_str = str(unique_id) if unique_id is not None else "0"
    retries_left = max_retries
    raw_stdout_lines = []
    img_tensor = torch.zeros((1, 64, 64, 3))
    out_dir = ""
    img_file_path = None
    
    is_final_success = False
    is_timeout_killed = False
    
    # 预估值，等待 Runner 解析后覆盖
    total_frames = len(job_data["frames_list"]) if job_data.get("frames_list") else 250
    actual_start_frame = job_data.get("start_frame", 1)
    actual_end_frame = job_data.get("end_frame", 250)
    actual_step = job_data.get("step", 1)

    # === 外层重试循环 ===
    while True:
        tmp_dir = tempfile.gettempdir()
        job_id = f"utsuki_{int(time.time()*1000)}"
        job_path = os.path.join(tmp_dir, f"{job_id}.json")
        progress_path = job_path + ".progress"
        runner_path = os.path.join(tmp_dir, f"{job_id}_runner.py")

        with open(runner_path, "w", encoding="utf-8") as f: f.write(RUNNER_SOURCE)
        with open(job_path, "w", encoding="utf-8") as f: json.dump(job_data, f)

        cmd = [blender, "--background", blend_path, "--python", runner_path, "--", job_path]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        
        start_time = time.time()
        current_frame = -1
        last_progress_time = time.time()
        running = True

        # 线程1：读取终端日志
        def read_output():
            nonlocal out_dir, img_file_path, total_frames, actual_start_frame, actual_end_frame, actual_step
            buffer = ""
            while True:
                try: char = p.stdout.read(1)
                except: break
                if not char: break
                if char == '\n' or char == '\r':
                    if buffer.strip():
                        line = buffer.strip()
                        raw_stdout_lines.append(line)
                        
                        if line.startswith("UTSUKI_OUT_DIR="):
                            out_dir = line.replace("UTSUKI_OUT_DIR=", "").strip()
                        if line.startswith("UTSUKI_TOTAL_FRAMES="):
                            try: total_frames = int(line.replace("UTSUKI_TOTAL_FRAMES=", "").strip())
                            except: pass
                        if line.startswith("UTSUKI_RANGE="):
                            try:
                                s_f, e_f, s_p = map(int, line.replace("UTSUKI_RANGE=", "").strip().split(","))
                                if len(raw_stdout_lines) < 100:
                                    actual_start_frame, actual_end_frame, actual_step = s_f, e_f, s_p
                                    total_frames = len(range(s_f, e_f + 1, s_p))
                            except: pass
                            
                        m_saved = re.search(r"Saved:\s*['\"]([^'\"]+\.(png|jpg|jpeg|exr|webp))['\"]", line, re.IGNORECASE)
                        if m_saved and not img_file_path:
                            img_file_path = m_saved.group(1)
                    buffer = ""
                else:
                    buffer += char

        # 线程2：监控进度文件、心跳超时检查、推送前端
        def monitor_progress():
            nonlocal current_frame, last_progress_time, is_timeout_killed
            last_pushed_idx = -1
            while running and p.poll() is None:
                now = time.time()
                
                # 超时强杀逻辑
                if timeout_minutes > 0 and (now - last_progress_time) > timeout_minutes * 60:
                    is_timeout_killed = True
                    p.kill()
                    break
                
                if os.path.exists(progress_path):
                    try:
                        with open(progress_path, "r") as pf:
                            content = pf.read().strip()
                            if content:
                                new_frame = int(content)
                                if new_frame != current_frame:
                                    current_frame = new_frame
                                    last_progress_time = time.time() # 重置心跳
                                    
                                    # 计算当前是第几张图
                                    if job_data.get("frames_list"):
                                        if current_frame in job_data["frames_list"]:
                                            idx = job_data["frames_list"].index(current_frame) + 1
                                        else:
                                            idx = 1
                                    else:
                                        idx = len(range(actual_start_frame, min(current_frame + 1, actual_end_frame + 1), actual_step))

                                    if idx != last_pushed_idx:
                                        last_pushed_idx = idx
                                        elapsed = time.time() - start_time
                                        fps = idx / elapsed if elapsed > 0 else 0
                                        eta = (total_frames - idx) / fps if fps > 0 and idx <= total_frames else 0
                                        
                                        PromptServer.instance.send_sync("utsuki_progress", {
                                            "node_id": node_id_str,
                                            "relative_frame": idx,
                                            "total": total_frames,
                                            "current_frame": current_frame,
                                            "elapsed": f"{int(elapsed)}s",
                                            "fps": f"{fps:.2f} f/s",
                                            "eta": f"{int(eta)}s"
                                        })
                    except Exception:
                        pass
                time.sleep(0.3)

        t1 = threading.Thread(target=read_output)
        t2 = threading.Thread(target=monitor_progress)
        t1.start()
        t2.start()

        try:
            p.wait()
        except Exception as e:
            raw_stdout_lines.append(f"节点异常: {e}")
        finally:
            running = False
            t1.join(timeout=1)
            t2.join(timeout=1)
            p.terminate()
            for pth in [job_path, progress_path, runner_path]:
                if os.path.exists(pth): os.remove(pth)

        if is_timeout_killed:
            break

        # 判断是否成功
        if p.returncode == 0 and "UTSUKI_FINISH_RENDER" in "\n".join(raw_stdout_lines):
            is_final_success = True
            break

        # 走到这里说明异常崩溃了，判断是否还有重试次数
        if retries_left > 0:
            retries_left -= 1
            raw_stdout_lines.append(f"\n[重试] 检测到中断，剩余 {retries_left} 次机会。")
            
            # 剔除已渲染帧，更新续传起点
            if job_data.get("frames_list"):
                if current_frame in job_data["frames_list"]:
                    idx = job_data["frames_list"].index(current_frame)
                    job_data["frames_list"] = job_data["frames_list"][idx + 1:]
                else:
                    job_data["frames_list"] = []
            else:
                job_data["start_frame"] = current_frame + actual_step
                
            if not job_data.get("frames_list") and "frames_list" in job_data:
                # 如果是单帧模式且全渲染完了，当作成功
                is_final_success = True
                break
            continue
        else:
            break

    # ==========================================
    # 整理返回结果
    # ==========================================
    raw_stdout = "\n".join(raw_stdout_lines)
    is_error = not is_final_success and not is_timeout_killed

    node_log = ""
    if is_timeout_killed:
        node_log += f"[超时终止] 渲染超过 {timeout_minutes} 分钟无进展，已强制终止。\n"
    elif is_error:
        node_log += f"[Utsuki Blender] 渲染失败！(重试耗尽或发生错误)\n"
        if "UTSUKI_PYTHON_ERROR=" in raw_stdout:
            node_log += f"脚本参数错误: {raw_stdout.split('UTSUKI_PYTHON_ERROR=')[1].split(chr(10))[0]}\n"
    else:
        node_log += "[Utsuki Blender] 渲染进程结束\n"
        if out_dir: node_log += f"输出目录：{out_dir}\n"

    if not is_error and not is_timeout_killed and img_file_path and os.path.exists(img_file_path):
        try:
            img = Image.open(img_file_path).convert("RGB")
            img_np = np.array(img).astype(np.float32) / 255.0
            img_tensor = torch.from_numpy(img_np)[None,]
            node_log += f"成功加载预览图：{os.path.basename(img_file_path)}\n"
        except Exception as e:
            node_log += f"[Warning] 读取图失败: {e}\n"

    return (node_log, raw_stdout, img_tensor)


# ============================================================
# ComfyUI 节点注册
# ============================================================
class UtsukiBlendFile:
    CATEGORY = "Utsuki/Blender"
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"blend_path": ("STRING", {"multiline": False, "default": ""})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("blend_path",)
    FUNCTION = "output_path"
    OUTPUT_NODE = False
    def output_path(self, blend_path):
        return (blend_path.strip().strip('"').strip("'"),)

class UtsukiBlenderAnimationRender:
    CATEGORY = "Utsuki/Blender"
    @classmethod
    def IS_CHANGED(cls, **kwargs): return time.time()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "blend_path": ("STRING", {"multiline": False, "default": ""}),
                "render_engine": (["默认", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"], {"default": "默认"}),
                "camera_name": ("STRING", {"multiline": False, "default": ""}),
                "start_frame": ("INT", {"default": -1, "min": -1, "max": 99999, "step": 1}),
                "end_frame": ("INT", {"default": -1, "min": -1, "max": 99999, "step": 1}),
                "step": ("INT", {"default": 1, "min": -1, "max": 1000, "step": 1}),
                "res_x": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_y": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_percent": ("INT", {"default": -1, "min": -1, "max": 1000, "step": 1}),
                "timeout_minutes": ("INT", {"default": 0, "min": 0, "max": 9999, "step": 1}),
                "max_retries": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "custom_params": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {"any_input": ("*", {})},
            "hidden": {"unique_id": "UNIQUE_ID"}
        }

    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("node_log", "blender_log", "image")
    FUNCTION = "render"

    def render(self, blend_path, render_engine, camera_name, start_frame, end_frame, step, res_x, res_y, res_percent, timeout_minutes, max_retries, custom_params, any_input=None, unique_id=None, **kwargs):
        blend_path = os.path.normpath(blend_path.strip().strip('"').strip("'")) if blend_path else ""
        job_data = {
            "engine": render_engine if render_engine != "默认" else None,
            "camera_name": camera_name,
            "start_frame": start_frame, "end_frame": end_frame, "step": step,
            "res_x": res_x, "res_y": res_y, "res_percent": res_percent,
            "custom_params": custom_params
        }
        return execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id)


class UtsukiBlenderFrameRender:
    CATEGORY = "Utsuki/Blender"
    @classmethod
    def IS_CHANGED(cls, **kwargs): return time.time()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "blend_path": ("STRING", {"multiline": False, "default": ""}),
                "render_engine": (["默认", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"], {"default": "默认"}),
                "camera_name": ("STRING", {"multiline": False, "default": ""}),
                "frames_input": ("STRING", {"multiline": True, "default": "1, 5, 10", "tooltip": "输入需要渲染的帧号，用英文逗号隔开。支持空格和换行。"}),
                "res_x": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_y": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_percent": ("INT", {"default": -1, "min": -1, "max": 1000, "step": 1}),
                "timeout_minutes": ("INT", {"default": 0, "min": 0, "max": 9999, "step": 1}),
                "max_retries": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "custom_params": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {"any_input": ("*", {})},
            "hidden": {"unique_id": "UNIQUE_ID"}
        }

    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("node_log", "blender_log", "image")
    FUNCTION = "render"

    def render(self, blend_path, render_engine, camera_name, frames_input, res_x, res_y, res_percent, timeout_minutes, max_retries, custom_params, any_input=None, unique_id=None, **kwargs):
        blend_path = os.path.normpath(blend_path.strip().strip('"').strip("'")) if blend_path else ""
        
        # 解析字符串：兼容中文逗号、英文逗号、空格、换行
        raw_tokens = re.split(r'[,\s，]+', frames_input.strip())
        frames_list = []
        for token in raw_tokens:
            if token:
                try: frames_list.append(int(token))
                except ValueError: pass

        if not frames_list:
            return ("[Error] 请输入至少一个有效的帧号。", "", torch.zeros((1, 64, 64, 3)))

        job_data = {
            "engine": render_engine if render_engine != "默认" else None,
            "camera_name": camera_name,
            "frames_list": frames_list,
            "res_x": res_x, "res_y": res_y, "res_percent": res_percent,
            "custom_params": custom_params
        }
        return execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id)


NODE_CLASS_MAPPINGS = {
    "UtsukiBlenderAnimationRender": UtsukiBlenderAnimationRender,
    "UtsukiBlenderFrameRender": UtsukiBlenderFrameRender,
    "UtsukiBlendFile": UtsukiBlendFile,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "UtsukiBlenderAnimationRender": "Blender Animation Render",
    "UtsukiBlenderFrameRender": "Blender Frame Render",
    "UtsukiBlendFile": "Blend File",
}