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
import requests
import hmac
import hashlib
import base64
from PIL import Image
from server import PromptServer
import comfy.model_management as mm
import folder_paths

# ============================================================
# 飞书通知功能辅助函数
# ============================================================
def format_duration(sec):
    """将秒数格式化为时/分/秒"""
    sec = int(max(0, sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}小时{m}分{s}秒"
    elif m > 0:
        return f"{m}分{s}秒"
    else:
        return f"{s}秒"

def send_feishu_notification(url, secret, notice_type, file_path, node_type, duration_sec, last_frame, log_lines, extra_info=""):
    if not url or not url.strip():
        return None

    try:
        url = url.strip()
        secret = secret.strip() if secret else ""
        
        payload = {}
        if secret:
            timestamp = str(int(time.time()))
            string_to_sign = f'{timestamp}\n{secret}'
            hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = base64.b64encode(hmac_code).decode('utf-8')
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        if notice_type == "success":
            title_color, title_text = "green", "✅ Blender 渲染成功"
        elif notice_type == "skip":
            title_color, title_text = "green", "✅ Blender 渲染已提前完成"
        elif notice_type == "retry":
            title_color, title_text = "orange", "⚠️ Blender 渲染崩溃，正在重试"
        elif notice_type == "failed":
            title_color, title_text = "red", "❌ Blender 渲染彻底失败"
        elif notice_type == "timeout":
            title_color, title_text = "red", "⏰ Blender 渲染超时终止"
        else:
            title_color, title_text = "grey", "Blender 渲染通知"

        log_summary = ""
        if log_lines:
            log_summary = "\n".join(log_lines[-5:])
            if len(log_summary) > 300:
                log_summary = log_summary[-300:] + "..."

        time_str = format_duration(duration_sec)
        frame_str = f"第 {last_frame} 帧" if last_frame != -1 else "无（尚未完成任何帧）"
        
        card_elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**文件:** `{os.path.basename(file_path)}`\n**类型:** {node_type}\n**最后渲染帧:** {frame_str}\n**渲染时间:** {time_str}\n{extra_info}"}}
        ]
        
        if log_summary and notice_type in ["retry", "failed", "timeout"]:
            card_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**错误摘要:**\n```text\n{log_summary}\n```"}})

        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title_text}, "template": title_color},
            "elements": card_elements
        }
            
        payload["msg_type"] = "interactive"
        payload["card"] = card_content

        headers = {'Content-Type': 'application/json'}
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        resp.raise_for_status()
        
        res_json = resp.json()
        if res_json.get("code") != 0:
            return f"飞书接口返回错误: {res_json.get('msg')} (Code: {res_json.get('code')})"
        return None
    except Exception as e:
        return f"网络或解析异常: {str(e)}"

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
# 子进程注入脚本 (Runner) - 移除相机相关代码
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
cache_path = job_path + ".cache"

try:
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    scene = bpy.context.scene

    if job.get("engine"): scene.render.engine = job["engine"]
    if job.get("res_x", -1) != -1: scene.render.resolution_x = job["res_x"]
    if job.get("res_y", -1) != -1: scene.render.resolution_y = job["res_y"]
    if job.get("res_percent", -1) != -1: scene.render.resolution_percentage = int(job["res_percent"])

    # 移除了 camera_name 判断

    if job.get("custom_params", "").strip():
        try: exec(job["custom_params"], {"bpy": bpy})
        except Exception as e:
            print(f"UTSUKI_CUSTOM_ERROR={str(e)}")
            sys.exit(1)

    out_dir = bpy.path.abspath(scene.render.filepath)
    print(f"UTSUKI_OUT_DIR={out_dir}")

    if job.get("force_start_frame", -1) != -1:
        scene.frame_start = job["force_start_frame"]
    if job.get("force_end_frame", -1) != -1:
        scene.frame_end = job["force_end_frame"]

    frames_list = job.get("frames_list")
    if not frames_list:
        if job.get("step", -1) != -1: scene.frame_step = job["step"]

    def write_progress(scene, *args):
        try:
            with open(progress_path, "w") as f:
                f.write(f"{scene.frame_current}")
            
            cache_data = {
                "last_frame": scene.frame_current,
                "real_start": scene.frame_start,
                "real_end": scene.frame_end,
                "step": scene.frame_step
            }
            with open(cache_path, "w") as cf:
                json.dump(cache_data, cf)
        except:
            pass

    bpy.app.handlers.render_post.append(write_progress)
    print("UTSUKI_START_RENDER")

    if frames_list:
        print(f"UTSUKI_TOTAL_FRAMES={len(frames_list)}")
        original_filepath = scene.render.filepath
        for f in frames_list:
            scene.frame_set(int(f))
            scene.render.filepath = original_filepath
            try:
                resolved_path = scene.render.frame_path(frame=int(f))
                scene.render.filepath = resolved_path
            except Exception:
                pass
            write_progress(scene)
            bpy.ops.render.render(write_still=True)
        scene.render.filepath = original_filepath
    else:
        write_progress(scene)
        print(f"UTSUKI_RANGE={scene.frame_start},{scene.frame_end},{scene.frame_step}")
        bpy.ops.render.render(animation=True)

    print("UTSUKI_FINISH_RENDER")

except Exception as e:
    print(f"UTSUKI_PYTHON_ERROR={str(e)}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
'''

def execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id, persist_id, feishu_url, feishu_secret, node_type="Animation", render_mode="重启 (Restart)"):
    blender = find_default_blender() or shutil.which("blender") or shutil.which("blender.exe")
    if not blender: 
        return ("[Error] 未能定位 Blender。", "", torch.zeros((1, 64, 64, 3)))

    node_id_str = str(unique_id) if unique_id is not None else "0"
    retries_left = max_retries
    raw_stdout_lines = []
    img_tensor = torch.zeros((1, 64, 64, 3))
    out_dir = ""
    img_file_path = None
    
    is_final_success = False
    is_timeout_killed = False
    is_user_canceled = False
    is_skipped = False
    
    start_time_global = time.time()

    temp_dir = folder_paths.get_temp_directory()
    cache_dir = os.path.join(temp_dir, "utsuki_blender_cache")
    os.makedirs(cache_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(blend_path))[0]
    pid = persist_id if persist_id else "default"
    cache_file = os.path.join(cache_dir, f"{base_name}_{pid}_{node_id_str}_info.json")

    node_start = job_data.get("start_frame", -1)
    node_end = job_data.get("end_frame", -1)
    node_step = job_data.get("step", 1)
    is_frames_list_mode = bool(job_data.get("frames_list"))

    force_start = node_start
    force_end = node_end

    def get_current_last_frame():
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    return json.load(f).get("last_frame", -1)
            except:
                pass
        return -1

    if render_mode.startswith("重启"):
        if os.path.exists(cache_file): os.remove(cache_file)
    else:
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cache_data = json.load(f)
                    last_frame = cache_data.get("last_frame", -1)
                    real_end = cache_data.get("real_end", -1)
                    cache_step = cache_data.get("step", node_step)
                    
                    effective_end = node_end if node_end != -1 else real_end
                    
                    if last_frame != -1 and effective_end != -1 and last_frame >= effective_end:
                        raw_stdout_lines.append(f"[继续渲染] 缓存显示上次已渲染至第 {last_frame} 帧，已经达到或超过结束帧 {effective_end}，无需重复渲染。")
                        is_final_success = True
                        is_skipped = True
                    elif last_frame != -1:
                        if node_start != -1 and last_frame <= node_start:
                            raw_stdout_lines.append(f"[继续渲染] 缓存进度 ({last_frame}) 未能超越节点起点 ({node_start})，将从 {node_start} 重新开始。")
                            force_start = node_start
                        else:
                            force_start = last_frame + cache_step
                            raw_stdout_lines.append(f"[继续渲染] 检测到历史进度，已渲染至第 {last_frame} 帧。将从 {force_start} 继续。")
                            
                        if is_frames_list_mode:
                            flist = job_data.get("frames_list", [])
                            job_data["frames_list"] = [f for f in flist if f > last_frame]
            except Exception as e:
                raw_stdout_lines.append(f"[Warning] 读取缓存失败: {e}，将重新渲染。")
                if os.path.exists(cache_file): os.remove(cache_file)

    if is_skipped:
        raw_stdout = "\n".join(raw_stdout_lines)
        elapsed = time.time() - start_time_global
        if feishu_url and feishu_url.strip():
            send_feishu_notification(feishu_url, feishu_secret, "skip", blend_path, node_type, elapsed, get_current_last_frame(), [], "任务已提前完成，无需重试。")
        return (raw_stdout, raw_stdout, torch.zeros((1, 64, 64, 3)))

    while True:
        tmp_dir = tempfile.gettempdir()
        job_id = f"utsuki_{int(time.time()*1000)}"
        job_path = os.path.join(tmp_dir, f"{job_id}.json")
        progress_path = job_path + ".progress"
        runner_path = os.path.join(tmp_dir, f"{job_id}_runner.py")

        if is_frames_list_mode and not job_data.get("frames_list"):
            is_final_success = True
            break

        job_data["force_start_frame"] = force_start
        job_data["force_end_frame"] = force_end

        with open(runner_path, "w", encoding="utf-8") as f: f.write(RUNNER_SOURCE)
        with open(job_path, "w", encoding="utf-8") as f: json.dump(job_data, f)

        runner_content = open(runner_path, "r", encoding="utf-8").read()
        runner_content = runner_content.replace(
            'cache_path = job_path + ".cache"',
            f'cache_path = r"{cache_file}"'
        )
        with open(runner_path, "w", encoding="utf-8") as f: f.write(runner_content)

        cmd = [blender, "--background", blend_path, "--python", runner_path, "--", job_path]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=creation_flags)
        
        last_progress_time = time.time()
        running = True

        def read_output():
            nonlocal out_dir, img_file_path
            buffer = ""
            while True:
                try: 
                    char = p.stdout.read(1)
                except: break
                if not char: break
                if mm.processing_interrupted(): break

                if char == '\n' or char == '\r':
                    if buffer.strip():
                        line = buffer.strip()
                        raw_stdout_lines.append(line)
                        
                        if line.startswith("UTSUKI_OUT_DIR="):
                            out_dir = line.replace("UTSUKI_OUT_DIR=", "").strip()
                        
                        m_saved = re.search(r"Saved:\s*['\"]([^'\"]+\.(png|jpg|jpeg|exr|webp))['\"]", line, re.IGNORECASE)
                        if m_saved and not img_file_path: img_file_path = m_saved.group(1)

                        if m_saved or "Fra:" in line or "UTSUKI_START_RENDER" in line:
                            if os.path.exists(cache_file):
                                try:
                                    with open(cache_file, "r") as cf:
                                        cdata = json.load(cf)
                                        lf = cdata.get("last_frame", -1)
                                        rs = cdata.get("real_start", 1)
                                        re_end = cdata.get("real_end", 100)
                                        st = cdata.get("step", 1)
                                        
                                        if is_frames_list_mode:
                                            flist = job_data.get("frames_list", [])
                                            idx = flist.index(lf) + 1 if lf in flist else 1
                                            tot = len(flist)
                                        else:
                                            if lf != -1 and re_end != -1:
                                                tot = len(range(rs, re_end + 1, st))
                                                idx = len(range(rs, min(lf + 1, re_end + 1), st))
                                            else:
                                                tot, idx = 1, 1
                                                
                                        elapsed = time.time() - start_time_global
                                        if idx > 0 and elapsed > 0:
                                            avg_time = elapsed / idx
                                            eta = avg_time * (tot - idx)
                                        else:
                                            avg_time, eta = 0, 0
                                                
                                        PromptServer.instance.send_sync("utsuki_progress", {
                                            "node_id": node_id_str,
                                            "current": idx,
                                            "total": tot,
                                            "frame": lf,
                                            "elapsed": elapsed,
                                            "avg_time": avg_time,
                                            "eta": eta
                                        })
                                except: pass
                    buffer = ""
                else:
                    buffer += char

        def monitor_timeout():
            nonlocal last_progress_time, is_timeout_killed, is_user_canceled, running
            last_frame_seen = -1
            while running and p.poll() is None:
                if mm.processing_interrupted():
                    is_user_canceled = True
                    p.kill()
                    running = False
                    break
                    
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, "r") as f:
                            cdata = json.load(f)
                            curr_f = cdata.get("last_frame", -1)
                            if curr_f != -1 and curr_f != last_frame_seen:
                                last_frame_seen = curr_f
                                last_progress_time = time.time()
                    except: pass
                    
                now = time.time()
                if timeout_minutes > 0 and (now - last_progress_time) > timeout_minutes * 60:
                    is_timeout_killed = True
                    p.kill()
                    running = False
                    break
                time.sleep(0.5)

        t1 = threading.Thread(target=read_output)
        t2 = threading.Thread(target=monitor_timeout)
        t1.start()
        t2.start()

        try:
            p.wait()
        except Exception as e:
            raw_stdout_lines.append(f"节点异常: {e}")
        finally:
            running = False
            try: p.stdout.close()
            except: pass
            t1.join(timeout=1)
            t2.join(timeout=1)
            if p.poll() is None: p.kill()
            for pth in [job_path, progress_path, runner_path]:
                if os.path.exists(pth): os.remove(pth)

        if is_user_canceled:
            return ("[已取消] 用户主动中断了渲染。", "\n".join(raw_stdout_lines), torch.zeros((1, 64, 64, 3)))
        
        if is_timeout_killed:
            raw_stdout = "\n".join(raw_stdout_lines)
            elapsed = time.time() - start_time_global
            if feishu_url and feishu_url.strip():
                send_feishu_notification(feishu_url, feishu_secret, "timeout", blend_path, node_type, elapsed, get_current_last_frame(), raw_stdout_lines)
            return (f"[超时终止] 渲染超过 {timeout_minutes} 分钟无进展，已强制终止。", raw_stdout, torch.zeros((1, 64, 64, 3)))

        if p.returncode == 0 and "UTSUKI_FINISH_RENDER" in "\n".join(raw_stdout_lines):
            is_final_success = True
            break

        if retries_left > 0:
            retries_left -= 1
            raw_stdout_lines.append(f"\n[重试] 检测到中断，剩余 {retries_left} 次机会。")
            
            last_f = -1
            step_val = node_step
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as f:
                        cdata = json.load(f)
                        last_f = cdata.get("last_frame", -1)
                        step_val = cdata.get("step", node_step)
                except: pass

            elapsed = time.time() - start_time_global
            if feishu_url and feishu_url.strip():
                send_feishu_notification(feishu_url, feishu_secret, "retry", blend_path, node_type, elapsed, get_current_last_frame(), raw_stdout_lines)

            if is_frames_list_mode:
                if last_f != -1:
                    flist = job_data.get("frames_list", [])
                    job_data["frames_list"] = [f for f in flist if f > last_f]
            else:
                if last_f != -1:
                    force_start = last_f + step_val
            continue
        else:
            elapsed = time.time() - start_time_global
            if feishu_url and feishu_url.strip():
                send_feishu_notification(feishu_url, feishu_secret, "failed", blend_path, node_type, elapsed, get_current_last_frame(), raw_stdout_lines)
            break

    raw_stdout = "\n".join(raw_stdout_lines)
    is_error = not is_final_success

    node_log = ""
    if is_error:
        node_log += f"[Utsuki Blender] 渲染失败！(重试耗尽或发生错误)\n"
        if "UTSUKI_PYTHON_ERROR=" in raw_stdout:
            node_log += f"脚本参数错误: {raw_stdout.split('UTSUKI_PYTHON_ERROR=')[1].split(chr(10))[0]}\n"
    else:
        node_log += "[Utsuki Blender] 渲染进程结束\n"
        if out_dir: node_log += f"输出目录：{out_dir}\n"
        elapsed = time.time() - start_time_global
        if feishu_url and feishu_url.strip():
            send_feishu_notification(feishu_url, feishu_secret, "success", blend_path, node_type, elapsed, get_current_last_frame(), [])

    if not is_error and img_file_path and os.path.exists(img_file_path):
        try:
            img = Image.open(img_file_path).convert("RGB")
            img_np = np.array(img).astype(np.float32) / 255.0
            img_tensor = torch.from_numpy(img_np)[None,]
            node_log += f"成功加载预览图：{os.path.basename(img_file_path)}\n"
        except Exception as e:
            node_log += f"[Warning] 读取图失败: {e}\n"

    return (node_log, raw_stdout, img_tensor)


# ============================================================
# ComfyUI 节点注册 (移除了 camera_name)
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
                "render_mode": (["重启 (Restart)", "继续 (Resume)"], {"default": "重启 (Restart)"}),
                "render_engine": (["默认", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"], {"default": "默认"}),
                "start_frame": ("INT", {"default": -1, "min": -1, "max": 99999, "step": 1}),
                "end_frame": ("INT", {"default": -1, "min": -1, "max": 99999, "step": 1}),
                "step": ("INT", {"default": 1, "min": -1, "max": 1000, "step": 1}),
                "res_x": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_y": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_percent": ("INT", {"default": -1, "min": -1, "max": 1000, "step": 1}),
                "timeout_minutes": ("INT", {"default": 0, "min": 0, "max": 9999, "step": 1}),
                "max_retries": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "feishu_url": ("STRING", {"multiline": False, "default": "", "tooltip": "飞书机器人 Webhook 地址"}),
                "feishu_secret": ("STRING", {"multiline": False, "default": "", "tooltip": "飞书机器人签名校验密钥"}),
                "custom_params": ("STRING", {"multiline": True, "default": ""}),
                "persist_id": ("STRING", {"default": ""}),
            },
            "optional": {"any_input": ("*", {})},
            "hidden": {"unique_id": "UNIQUE_ID"}
        }

    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("node_log", "blender_log", "image")
    FUNCTION = "render"

    def render(self, blend_path, render_mode, render_engine, start_frame, end_frame, step, res_x, res_y, res_percent, timeout_minutes, max_retries, feishu_url, feishu_secret, custom_params, persist_id, any_input=None, unique_id=None, **kwargs):
        blend_path = os.path.normpath(blend_path.strip().strip('"').strip("'")) if blend_path else ""
        job_data = {
            "engine": render_engine if render_engine != "默认" else None,
            "start_frame": start_frame, "end_frame": end_frame, "step": step,
            "res_x": res_x, "res_y": res_y, "res_percent": res_percent,
            "custom_params": custom_params
        }
        return execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id, persist_id, feishu_url, feishu_secret, node_type="动画渲染", render_mode=render_mode)

class UtsukiBlenderFrameRender:
    CATEGORY = "Utsuki/Blender"
    @classmethod
    def IS_CHANGED(cls, **kwargs): return time.time()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "blend_path": ("STRING", {"multiline": False, "default": ""}),
                "render_mode": (["重启 (Restart)", "继续 (Resume)"], {"default": "重启 (Restart)"}),
                "render_engine": (["默认", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_WORKBENCH"], {"default": "默认"}),
                "frames_input": ("STRING", {"multiline": True, "default": "1, 5, 10"}),
                "res_x": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_y": ("INT", {"default": -1, "min": -1, "max": 8192, "step": 1}),
                "res_percent": ("INT", {"default": -1, "min": -1, "max": 1000, "step": 1}),
                "timeout_minutes": ("INT", {"default": 0, "min": 0, "max": 9999, "step": 1}),
                "max_retries": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "feishu_url": ("STRING", {"multiline": False, "default": "", "tooltip": "飞书机器人 Webhook 地址"}),
                "feishu_secret": ("STRING", {"multiline": False, "default": "", "tooltip": "飞书机器人签名校验密钥"}),
                "custom_params": ("STRING", {"multiline": True, "default": ""}),
                "persist_id": ("STRING", {"default": ""}),
            },
            "optional": {"any_input": ("*", {})},
            "hidden": {"unique_id": "UNIQUE_ID"}
        }

    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("node_log", "blender_log", "image")
    FUNCTION = "render"

    def render(self, blend_path, render_mode, render_engine, frames_input, res_x, res_y, res_percent, timeout_minutes, max_retries, feishu_url, feishu_secret, custom_params, persist_id, any_input=None, unique_id=None, **kwargs):
        blend_path = os.path.normpath(blend_path.strip().strip('"').strip("'")) if blend_path else ""
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
            "frames_list": frames_list,
            "res_x": res_x, "res_y": res_y, "res_percent": res_percent,
            "custom_params": custom_params
        }
        return execute_blender_render(job_data, blend_path, timeout_minutes, max_retries, unique_id, persist_id, feishu_url, feishu_secret, node_type="单帧渲染", render_mode=render_mode)

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