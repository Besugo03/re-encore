import os
import json
import time
import shlex
import threading
import subprocess
import shutil
import gradio as gr

# ==========================================
# CONFIGURATION & PERSISTENCE
# ==========================================
DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
THUMBNAIL_PATH = os.path.join(DATA_DIR, "current_thumb.jpg")

state = {
    "watched_folders": [],
    "processed_dir": "",
    "custom_flags": "--preset 6",
    "auto_delete": False,
    "handle_efficient": True,  # New flag to handle already compressed files
}

QUEUE = []
COMPLETED = []
FAILED = []
CURRENTLY_PROCESSING = None
CURRENT_PROCESS = None

# New Log Buffers for perfect Progress Bar rendering
LOG_LINES = []
CURRENT_LINE = []

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".ts",
    ".flv",
    ".wmv",
}


def load_data():
    global COMPLETED, FAILED
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                state.update(json.load(f))
        except Exception:
            pass
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                hist = json.load(f)
                COMPLETED = hist.get("completed", [])
                FAILED = hist.get("failed", [])
        except Exception:
            pass


def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(state, f, indent=4)


def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump({"completed": COMPLETED, "failed": FAILED}, f, indent=4)


load_data()


def get_output_path(input_path, watch_folder, processed_dir):
    watch_base = os.path.basename(os.path.normpath(watch_folder))
    rel_path = os.path.relpath(input_path, watch_folder)
    out_path = os.path.join(processed_dir, watch_base, rel_path)
    base, _ = os.path.splitext(out_path)
    # Always target MKV to ensure maximum compatibility for AV1
    return f"{base}-encoded.mkv"


def generate_thumbnail(video_path):
    """Uses FFmpeg to grab a preview frame from the 2 second mark."""
    if os.path.exists(THUMBNAIL_PATH):
        try:
            os.remove(THUMBNAIL_PATH)
        except:
            pass
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            "2",
            "-i",
            video_path,
            "-vframes",
            "1",
            "-q:v",
            "2",
            THUMBNAIL_PATH,
        ]
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )
    except Exception:
        pass


# ==========================================
# BACKGROUND WORKERS
# ==========================================
def watcher_thread():
    """Continuously scans watched folders for new video files."""
    global QUEUE, COMPLETED
    while True:
        if state["processed_dir"] and os.path.exists(state["processed_dir"]):
            for folder in state["watched_folders"]:
                if not os.path.exists(folder):
                    continue
                for root, dirs, files in os.walk(folder):
                    for file in files:
                        ext = os.path.splitext(file)[1].lower()
                        if ext in VIDEO_EXTENSIONS:
                            input_path = os.path.join(root, file)
                            out_path = get_output_path(
                                input_path, folder, state["processed_dir"]
                            )

                            is_completed = any(
                                c["input"] == input_path for c in COMPLETED
                            )

                            if is_completed and not os.path.exists(out_path):
                                COMPLETED = [
                                    c for c in COMPLETED if c["input"] != input_path
                                ]
                                save_history()
                                is_completed = False

                            if is_completed:
                                continue

                            in_queue = any(q["input"] == input_path for q in QUEUE)
                            is_failed = any(f["input"] == input_path for f in FAILED)

                            if not (in_queue or is_failed):
                                QUEUE.append(
                                    {
                                        "input": input_path,
                                        "output": out_path,
                                        "watch_folder": folder,
                                    }
                                )
        time.sleep(5)


def processor_thread():
    """Pulls jobs from the queue and runs the ab-av1 encoding command."""
    global QUEUE, COMPLETED, FAILED, CURRENTLY_PROCESSING, CURRENT_PROCESS
    global LOG_LINES, CURRENT_LINE

    while True:
        if QUEUE and not CURRENTLY_PROCESSING:
            CURRENTLY_PROCESSING = QUEUE[0]

            CURRENTLY_PROCESSING["output"] = get_output_path(
                CURRENTLY_PROCESSING["input"],
                CURRENTLY_PROCESSING["watch_folder"],
                state["processed_dir"],
            )

            in_file = CURRENTLY_PROCESSING["input"]
            out_file = CURRENTLY_PROCESSING["output"]
            flags = state["custom_flags"]

            generate_thumbnail(in_file)

            LOG_LINES.clear()
            CURRENT_LINE.clear()
            LOG_LINES.append(f"Starting process for {os.path.basename(in_file)}...\n")

            os.makedirs(os.path.dirname(out_file), exist_ok=True)

            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except:
                    pass

            cmd = [
                "ab-av1",
                "auto-encode",
                "-i",
                in_file,
                "--min-vmaf",
                "96",
                "-o",
                out_file,
            ]
            if flags.strip():
                cmd.extend(shlex.split(flags))

            LOG_LINES.append(f"Command: {' '.join(cmd)}\n")

            try:
                CURRENT_PROCESS = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )

                last_was_cr = False
                while True:
                    char = CURRENT_PROCESS.stdout.read(1)
                    if not char and CURRENT_PROCESS.poll() is not None:
                        break
                    if char:
                        if char == "\r":
                            last_was_cr = True
                        elif char == "\n":
                            LOG_LINES.append("".join(CURRENT_LINE))
                            CURRENT_LINE.clear()
                            last_was_cr = False
                            if len(LOG_LINES) > 300:
                                LOG_LINES = LOG_LINES[-300:]
                        else:
                            if last_was_cr:
                                CURRENT_LINE.clear()
                                last_was_cr = False
                            CURRENT_LINE.append(char)

                CURRENT_PROCESS.wait()

                full_log = "\n".join(LOG_LINES)
                if CURRENT_LINE:
                    full_log += "\n" + "".join(CURRENT_LINE)

                is_success = False
                skip_auto_delete = False  # Tracks if we've already moved the file and bypassed the need to delete

                if CURRENT_PROCESS.returncode == 0:
                    is_success = True
                else:
                    log_lower = full_log.lower()
                    skip_errors = [
                        "failed to find suitable crf",
                        "predicting encode will be larger than input",
                    ]

                    if CURRENT_PROCESS.returncode < 0:
                        CURRENTLY_PROCESSING["error"] = "Force Stopped by User"
                        CURRENTLY_PROCESSING["failed_log"] = full_log
                        FAILED.append(CURRENTLY_PROCESSING)
                        save_history()
                        if os.path.exists(out_file):
                            try:
                                os.remove(out_file)
                            except:
                                pass

                    elif any(err in log_lower for err in skip_errors):
                        if state.get("handle_efficient", True):
                            orig_ext = os.path.splitext(in_file)[1]
                            out_file_copy = os.path.splitext(out_file)[0] + orig_ext

                            # Clean up target just in case
                            if os.path.exists(out_file_copy):
                                try:
                                    os.remove(out_file_copy)
                                except:
                                    pass

                            try:
                                if state.get("auto_delete"):
                                    LOG_LINES.append(
                                        "\n⚠️ Original video is already highly efficient. MOVING to processed folder...\n"
                                    )
                                    shutil.move(in_file, out_file_copy)
                                    skip_auto_delete = True  # File is already gone, no need to delete later
                                else:
                                    LOG_LINES.append(
                                        "\n⚠️ Original video is already highly efficient. COPYING to processed folder...\n"
                                    )
                                    shutil.copy2(in_file, out_file_copy)

                                CURRENTLY_PROCESSING["output"] = out_file_copy
                                is_success = True
                            except Exception as e:
                                LOG_LINES.append(
                                    f"\n❌ Failed to move/copy file: {e}\n"
                                )
                                CURRENTLY_PROCESSING["error"] = f"Move/Copy failed: {e}"
                                CURRENTLY_PROCESSING["failed_log"] = (
                                    full_log + f"\nMove/Copy failed: {e}"
                                )
                                FAILED.append(CURRENTLY_PROCESSING)
                                save_history()
                        else:
                            LOG_LINES.append(
                                "\n⚠️ Original video is already highly efficient, but Auto-Move/Copy is disabled. Skipping...\n"
                            )
                            CURRENTLY_PROCESSING["error"] = (
                                "Already Highly Compressed (Skipped)"
                            )
                            CURRENTLY_PROCESSING["failed_log"] = full_log
                            FAILED.append(CURRENTLY_PROCESSING)
                            save_history()
                    else:
                        CURRENTLY_PROCESSING["error"] = (
                            f"Exit code {CURRENT_PROCESS.returncode}"
                        )
                        CURRENTLY_PROCESSING["failed_log"] = full_log
                        FAILED.append(CURRENTLY_PROCESSING)
                        save_history()
                        if os.path.exists(out_file):
                            try:
                                os.remove(out_file)
                            except:
                                pass

                if is_success:
                    COMPLETED.append(CURRENTLY_PROCESSING)
                    save_history()

                    if state.get("auto_delete") and not skip_auto_delete:
                        try:
                            os.remove(in_file)
                            LOG_LINES.append(
                                f"\n🗑️ Auto-deleted original file: {in_file}\n"
                            )
                        except Exception as e:
                            LOG_LINES.append(
                                f"\n⚠️ Failed to auto-delete original file: {e}\n"
                            )

            except Exception as e:
                CURRENTLY_PROCESSING["error"] = str(e)
                CURRENTLY_PROCESSING["failed_log"] = (
                    "\n".join(LOG_LINES) + f"\n❌ System Error: {e}"
                )
                FAILED.append(CURRENTLY_PROCESSING)
                save_history()

            if QUEUE and QUEUE[0]["input"] == CURRENTLY_PROCESSING["input"]:
                QUEUE.pop(0)

            CURRENT_PROCESS = None
            CURRENTLY_PROCESSING = None
        else:
            time.sleep(2)


threading.Thread(target=watcher_thread, daemon=True).start()
threading.Thread(target=processor_thread, daemon=True).start()

# ==========================================
# GRADIO USER INTERFACE
# ==========================================
with gr.Blocks(title="ab-av1 Auto-Encoder") as demo:
    gr.Markdown("# 🎬 ab-av1 Auto-Encoder Dashboard")

    with gr.Tab("Dashboard"):
        with gr.Row(equal_height=False):
            with gr.Column(scale=2):
                with gr.Row(equal_height=False):
                    with gr.Column():
                        status_text = gr.Markdown("### Status: 💤 Idle")
                        force_stop_btn = gr.Button(
                            "🛑 Force Stop Current Encode", size="sm", variant="stop"
                        )
                    with gr.Column():
                        thumb_img = gr.Image(
                            label="Current Job Preview", interactive=False, height=120
                        )

                with gr.Group():
                    gr.Markdown("### ⚡ Force Add File/Folder")
                    with gr.Row():
                        force_add_in = gr.Textbox(
                            placeholder="Enter exact path to a file or folder to force-queue...",
                            show_label=False,
                            scale=4,
                        )
                        force_add_btn = gr.Button("🚀 Force Add", scale=1)
                    force_add_status = gr.Markdown("")

                gr.Markdown("### ⏳ Queue")
                queue_df = gr.Dataframe(
                    headers=["Input File", "Target Output"], interactive=False
                )

                with gr.Row():
                    gr.Markdown("### ✅ Completed")
                    clear_completed_btn = gr.Button(
                        "🧹 Clear Completed History", size="sm", variant="secondary"
                    )
                comp_df = gr.Dataframe(
                    headers=["Input File", "Output File"], interactive=False
                )

                # --- Failed Table Controls ---
                gr.Markdown("### ❌ Failed & Stopped")
                with gr.Row():
                    retry_all_btn = gr.Button("🔄 Retry All Failed Jobs", size="sm")
                    resume_stopped_btn = gr.Button(
                        "▶️ Resume Stopped Encodes", size="sm"
                    )

                fail_df = gr.Dataframe(
                    headers=["Input File", "Error"], interactive=False
                )

                with gr.Row():
                    selected_failed = gr.Textbox(
                        label="Selected Failed File (Click a row above to select)",
                        interactive=False,
                        scale=3,
                    )
                    retry_selected_btn = gr.Button("🔄 Retry Selected", scale=1)

                failed_log_view = gr.TextArea(
                    label="Reason / Logs for Selected Failed Job",
                    lines=10,
                    interactive=False,
                    autoscroll=False,
                )

            with gr.Column(scale=1):
                gr.Markdown("### 📝 Live Process Logs")
                log_box = gr.TextArea(
                    lines=35, interactive=False, show_label=False, autoscroll=True
                )

    with gr.Tab("Settings & Folders"):
        gr.Markdown("## ⚙️ Configuration")
        with gr.Group():
            proc_dir_in = gr.Textbox(
                label="Processed Directory",
                value=state["processed_dir"],
                placeholder="/media/processed",
            )
            flags_in = gr.Textbox(
                label="Optional ab-av1 Flags",
                value=state["custom_flags"],
                placeholder="--preset 6 --pix-format yuv420p10le",
            )

            # --- New Checkboxes ---
            auto_delete_in = gr.Checkbox(
                label="🗑️ Auto-delete original video after successful processing",
                value=state.get("auto_delete", False),
            )
            handle_efficient_in = gr.Checkbox(
                label="⏩ Auto-Move/Copy video to processed folder if it is already highly compressed (Skips encoding)",
                value=state.get("handle_efficient", True),
            )

            with gr.Row():
                save_btn = gr.Button("💾 Save Settings", variant="primary")
                settings_status = gr.Markdown("")

        gr.Markdown("---")
        gr.Markdown("### 📁 Watched Folders")
        folders_state = gr.State(state["watched_folders"])

        with gr.Row():
            new_folder_in = gr.Textbox(
                label="Add New Watch Folder", placeholder="/media/movies", scale=4
            )
            add_btn = gr.Button("➕ Add Folder", scale=1)
            clear_btn = gr.Button("🗑️ Clear All", scale=1, variant="stop")

        folder_status = gr.Markdown("")
        folders_df = gr.Dataframe(
            value=[[f] for f in state["watched_folders"]],
            headers=["Folder Path"],
            column_count=1,
            interactive=False,
        )

    # --- Configuration Actions ---
    def save_settings(proc_dir, flags, auto_del, handle_eff, folders):
        proc_dir = proc_dir.strip()
        if proc_dir and not os.path.exists(proc_dir):
            try:
                os.makedirs(proc_dir)
            except Exception as e:
                return f"⚠️ Error creating processed directory: {e}"
        state["processed_dir"] = proc_dir
        state["custom_flags"] = flags
        state["auto_delete"] = auto_del
        state["handle_efficient"] = handle_eff
        state["watched_folders"] = folders
        save_config()
        return "✅ Settings saved!"

    save_btn.click(
        save_settings,
        inputs=[
            proc_dir_in,
            flags_in,
            auto_delete_in,
            handle_efficient_in,
            folders_state,
        ],
        outputs=[settings_status],
    )

    def add_folder(new_folder, folders):
        new_folder = new_folder.strip()
        if new_folder and os.path.isdir(new_folder):
            if new_folder not in folders:
                folders.append(new_folder)
                state["watched_folders"] = folders
                save_config()
                return folders, [[f] for f in folders], "", "✅ Folder added!"
            return folders, [[f] for f in folders], new_folder, "⚠️ Already watched."
        return folders, [[f] for f in folders], new_folder, "❌ Invalid path."

    add_btn.click(
        add_folder,
        inputs=[new_folder_in, folders_state],
        outputs=[folders_state, folders_df, new_folder_in, folder_status],
    )

    def clear_folders():
        state["watched_folders"] = []
        save_config()
        return [], [], "🗑️ Cleared!"

    clear_btn.click(
        clear_folders, inputs=[], outputs=[folders_state, folders_df, folder_status]
    )

    # --- Queue & Fail Logic Actions ---
    def retry_all_failed():
        global FAILED, QUEUE
        for f in FAILED:
            if not any(q["input"] == f["input"] for q in QUEUE):
                QUEUE.append(
                    {
                        "input": f["input"],
                        "output": f["output"],
                        "watch_folder": f["watch_folder"],
                    }
                )
        FAILED.clear()
        save_history()

    retry_all_btn.click(retry_all_failed, inputs=[], outputs=[])

    def resume_stopped():
        global FAILED, QUEUE
        to_keep = []
        for f in FAILED:
            if f.get("error") == "Force Stopped by User":
                if not any(q["input"] == f["input"] for q in QUEUE):
                    QUEUE.append(
                        {
                            "input": f["input"],
                            "output": f["output"],
                            "watch_folder": f["watch_folder"],
                        }
                    )
            else:
                to_keep.append(f)
        FAILED.clear()
        FAILED.extend(to_keep)
        save_history()

    resume_stopped_btn.click(resume_stopped, inputs=[], outputs=[])

    def select_failed_row(evt: gr.SelectData):
        row_idx = evt.index[0]
        if row_idx < len(FAILED):
            return FAILED[row_idx]["input"], FAILED[row_idx].get(
                "failed_log", "No logs available."
            )
        return "", ""

    fail_df.select(
        select_failed_row, inputs=[], outputs=[selected_failed, failed_log_view]
    )

    def retry_specific_file(file_path):
        global FAILED, QUEUE
        file_path = file_path.strip()
        if not file_path:
            return "", ""

        for i, f in enumerate(FAILED):
            if f["input"] == file_path:
                if not any(q["input"] == f["input"] for q in QUEUE):
                    QUEUE.append(
                        {
                            "input": f["input"],
                            "output": f["output"],
                            "watch_folder": f["watch_folder"],
                        }
                    )
                FAILED.pop(i)
                save_history()
                return "", ""
        return file_path, "Log not found."

    retry_selected_btn.click(
        retry_specific_file,
        inputs=[selected_failed],
        outputs=[selected_failed, failed_log_view],
    )

    def clear_completed():
        global COMPLETED
        COMPLETED.clear()
        save_history()

    clear_completed_btn.click(clear_completed, inputs=[], outputs=[])

    def force_stop():
        global CURRENT_PROCESS
        if CURRENT_PROCESS is not None:
            try:
                CURRENT_PROCESS.kill()
            except:
                pass

    force_stop_btn.click(force_stop, inputs=[], outputs=[])

    def force_add(path):
        global QUEUE
        path = path.strip()
        if not path or not os.path.exists(path):
            return "❌ Path does not exist or is empty."

        added = 0
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                matched_folder = os.path.dirname(path)
                for wf in state["watched_folders"]:
                    if path.startswith(wf):
                        matched_folder = wf
                        break
                out_path = get_output_path(path, matched_folder, state["processed_dir"])
                if not any(q["input"] == path for q in QUEUE):
                    QUEUE.append(
                        {
                            "input": path,
                            "output": out_path,
                            "watch_folder": matched_folder,
                        }
                    )
                    added += 1
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in VIDEO_EXTENSIONS:
                        input_path = os.path.join(root, file)
                        matched_folder = path
                        for wf in state["watched_folders"]:
                            if input_path.startswith(wf):
                                matched_folder = wf
                                break
                        out_path = get_output_path(
                            input_path, matched_folder, state["processed_dir"]
                        )
                        if not any(q["input"] == input_path for q in QUEUE):
                            QUEUE.append(
                                {
                                    "input": input_path,
                                    "output": out_path,
                                    "watch_folder": matched_folder,
                                }
                            )
                            added += 1

        if added > 0:
            return f"✅ Force added {added} file(s) to queue. (Bypassed history check)"
        else:
            return "⚠️ No supported video files found."

    force_add_btn.click(force_add, inputs=[force_add_in], outputs=[force_add_status])

    # --- Live Dashboard Tick ---
    def update_dash():
        if CURRENTLY_PROCESSING:
            status = f"### Status: 🔄 Processing `{os.path.basename(CURRENTLY_PROCESSING['input'])}`"
        else:
            status = "### Status: 💤 Idle"

        current_log = "\n".join(list(LOG_LINES))
        if CURRENT_LINE:
            current_log += "\n" + "".join(CURRENT_LINE)

        q_data = []
        for i, f in enumerate(QUEUE):
            if (
                i == 0
                and CURRENTLY_PROCESSING is not None
                and f["input"] == CURRENTLY_PROCESSING["input"]
            ):
                q_data.append(
                    [f"▶️ RUNNING: {os.path.basename(f['input'])}", f["output"]]
                )
            else:
                q_data.append([f["input"], f["output"]])

        c_data = [[f["input"], f["output"]] for f in COMPLETED]
        f_data = [[f["input"], f.get("error", "Unknown Error")] for f in FAILED]

        img_val = (
            THUMBNAIL_PATH
            if (CURRENTLY_PROCESSING and os.path.exists(THUMBNAIL_PATH))
            else None
        )

        return status, img_val, current_log, q_data, c_data, f_data

    timer = gr.Timer(1)
    timer.tick(
        update_dash,
        inputs=[],
        outputs=[status_text, thumb_img, log_box, queue_df, comp_df, fail_df],
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, theme=gr.themes.Soft())
