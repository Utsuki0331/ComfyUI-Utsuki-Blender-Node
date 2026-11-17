import asyncio
import os
import subprocess
import tempfile
from server import PromptServer
from aiohttp import web
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, find_default_blender

WEB_DIRECTORY = "./web"

@PromptServer.instance.routes.get("/utsuki/select_blend_files")
async def select_blend_files(request):
    def open_dialog():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            paths = filedialog.askopenfilenames(
                title="选择 Blender 文件（可多选）", 
                filetypes=[("Blender Files", "*.blend")]
            )
            root.destroy()
            return [p.replace("\\", "/") for p in paths] if paths else []
        except Exception:
            return []
    paths = await asyncio.to_thread(open_dialog)
    return web.json_response({"paths": paths})

@PromptServer.instance.routes.get("/utsuki/get_cameras")
async def get_cameras(request):
    blend_path = request.query.get("blend_path", "")
    if not blend_path or not os.path.isfile(blend_path):
        return web.json_response({"cameras": []})

    blender = find_default_blender()
    if not blender:
        return web.json_response({"cameras": []})

    temp_script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    temp_script.write("import bpy\ncams = [o.name for o in bpy.data.objects if o.type == 'CAMERA']\nprint('UTSUKI_CAMS=' + '||'.join(cams))\n")
    temp_script.close()

    try:
        result = subprocess.run([blender, "--background", blend_path, "--python", temp_script.name], 
                                capture_output=True, text=True, timeout=30)
        out = result.stdout
        m = [line for line in out.splitlines() if line.startswith("UTSUKI_CAMS=")]
        if m:
            cams = m[0].replace("UTSUKI_CAMS=", "").strip()
            return web.json_response({"cameras": [c for c in cams.split("||") if c]})
    except Exception:
        pass
    finally:
        if os.path.exists(temp_script.name):
            os.remove(temp_script.name)
            
    return web.json_response({"cameras": []})

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]