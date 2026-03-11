#!/usr/bin/env python3
"""GUI wrapper for lif2tiff.py — LIF → TIFF converter."""

import csv
import json
import queue
import sys
import threading
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox
import numpy as np
import tifffile

# Import converter functions
from lif2tiff import process_lif, get_channel_info
from summarize_metadata import extract_key_fields

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class ConversionWorker(threading.Thread):
    def __init__(self, lif_path, output_dir, dry_run, channels_filter, msg_queue):
        super().__init__(daemon=True)
        self.lif_path = lif_path
        self.output_dir = output_dir
        self.dry_run = dry_run
        self.channels_filter = channels_filter
        self.q = msg_queue

    def run(self):
        try:
            def progress(current, total, msg):
                self.q.put(("progress", current, total, msg))

            process_lif(
                self.lif_path,
                Path(self.output_dir),
                dry_run=self.dry_run,
                channels_filter=self.channels_filter if self.channels_filter else None,
                progress_callback=progress,
            )

            # Post-processing: validation and summary (only if not dry-run)
            if not self.dry_run:
                self.q.put(("progress", 1, 1, "Running validation..."))
                validation_result = self._validate_output()

                self.q.put(("progress", 1, 1, "Generating metadata summary..."))
                summary_result = self._summarize_metadata()

                self.q.put(("done", {"validation": validation_result, "summary": summary_result}))
            else:
                self.q.put(("done", None))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _validate_output(self):
        """Run validation on output directory."""
        try:
            output_dir = Path(self.output_dir)
            lif_dirs = [d for d in output_dir.iterdir() if d.is_dir()]

            all_results = []
            total_series = 0
            valid_series = 0

            for lif_dir in lif_dirs:
                series_dirs = [d for d in lif_dir.iterdir()
                              if d.is_dir() and d.name != "by_channel"]

                for series_dir in series_dirs:
                    total_series += 1
                    is_valid = self._validate_series(series_dir)
                    if is_valid:
                        valid_series += 1

                    all_results.append({
                        "lif": lif_dir.name,
                        "series": series_dir.name,
                        "valid": is_valid
                    })

            # Save validation report JSON
            report = {
                "summary": {
                    "total_series": total_series,
                    "valid_series": valid_series,
                    "failed_series": total_series - valid_series,
                    "success_rate": valid_series / total_series if total_series > 0 else 0
                },
                "results": all_results
            }

            report_path = output_dir / "validation_report.json"
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)

            return {
                "total": total_series,
                "valid": valid_series,
                "failed": total_series - valid_series,
                "report_path": str(report_path)
            }
        except Exception as e:
            return {"error": str(e)}

    def _validate_series(self, series_dir):
        """Validate a single series directory — only check files that actually exist."""
        metadata_path = series_dir / f"{series_dir.name}_metadata.json"
        if not metadata_path.exists():
            return False

        try:
            # Check at least one TIFF exists and is readable
            tiff_files = list(series_dir.glob("*.tif"))
            if not tiff_files:
                return False

            for tiff_path in tiff_files:
                with tifffile.TiffFile(tiff_path) as tif:
                    img = tif.asarray()
                    if img.size == 0:
                        return False

            return True
        except Exception:
            return False

    def _summarize_metadata(self):
        """Generate metadata CSV summary."""
        try:
            output_dir = Path(self.output_dir)
            metadata_files = []

            for lif_dir in output_dir.iterdir():
                if not lif_dir.is_dir():
                    continue
                for series_dir in lif_dir.iterdir():
                    if not series_dir.is_dir() or series_dir.name == "by_channel":
                        continue
                    metadata_path = series_dir / f"{series_dir.name}_metadata.json"
                    if metadata_path.exists():
                        metadata_files.append(metadata_path)

            if not metadata_files:
                return {"error": "No metadata files found"}

            # Write CSV
            csv_path = output_dir / "metadata_summary.csv"
            rows = []
            for mf in metadata_files:
                with open(mf) as f:
                    metadata = json.load(f)
                rows.append(extract_key_fields(metadata))

            if rows:
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)

            return {"csv_path": str(csv_path), "total_series": len(rows)}
        except Exception as e:
            return {"error": str(e)}



class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LIF → TIFF Converter")
        self.geometry("620x560")
        self.resizable(False, False)

        self._channel_vars = {}   # label -> BooleanVar
        self._channel_frame = None
        self._worker = None
        self._queue = queue.Queue()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 16, "pady": 6}

        # --- Input file ---
        row0 = ctk.CTkFrame(self, fg_color="transparent")
        row0.pack(fill="x", **pad)
        ctk.CTkLabel(row0, text="Input LIF:", width=90, anchor="w").pack(side="left")
        self._input_var = ctk.StringVar()
        ctk.CTkEntry(row0, textvariable=self._input_var, width=380).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row0, text="Browse", width=80, command=self._browse_input).pack(side="left")

        # --- Output dir ---
        row1 = ctk.CTkFrame(self, fg_color="transparent")
        row1.pack(fill="x", **pad)
        ctk.CTkLabel(row1, text="Output Dir:", width=90, anchor="w").pack(side="left")
        self._output_var = ctk.StringVar(value="./output")
        ctk.CTkEntry(row1, textvariable=self._output_var, width=380).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row1, text="Browse", width=80, command=self._browse_output).pack(side="left")

        # --- Options ---
        opts = ctk.CTkFrame(self)
        opts.pack(fill="x", padx=16, pady=4)

        self._dryrun_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Dry-run (preview only, no export)",
                        variable=self._dryrun_var).pack(anchor="w", padx=12, pady=6)

        # Channel section (populated after file load)
        ctk.CTkLabel(opts, text="Channels to export:", anchor="w").pack(anchor="w", padx=12)
        self._channel_frame = ctk.CTkFrame(opts, fg_color="transparent")
        self._channel_frame.pack(anchor="w", padx=24, pady=(0, 8))
        ctk.CTkLabel(self._channel_frame,
                     text="(select a LIF file to see channels)",
                     text_color="gray").pack(anchor="w")

        # --- Convert button ---
        self._convert_btn = ctk.CTkButton(self, text="Convert", height=40,
                                          command=self._on_convert)
        self._convert_btn.pack(padx=16, pady=8, fill="x")

        # --- Progress ---
        prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        prog_frame.pack(fill="x", padx=16, pady=(0, 4))
        self._progress_bar = ctk.CTkProgressBar(prog_frame)
        self._progress_bar.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self._progress_bar.set(0)
        self._progress_label = ctk.CTkLabel(prog_frame, text="0%", width=40)
        self._progress_label.pack(side="left")

        # --- Log ---
        self._log = ctk.CTkTextbox(self, height=180, state="disabled",
                                   font=("Courier", 12))
        self._log.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select LIF file",
            filetypes=[("Leica Image Files", "*.lif *.LIF"), ("All files", "*.*")]
        )
        if path:
            self._input_var.set(path)
            self._load_channels(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self._output_var.set(path)

    def _load_channels(self, lif_path):
        """Scan LIF file and populate channel checkboxes."""
        for w in self._channel_frame.winfo_children():
            w.destroy()
        self._channel_vars.clear()

        try:
            channels = get_channel_info(lif_path)
        except Exception as e:
            ctk.CTkLabel(self._channel_frame, text=f"Error reading file: {e}",
                         text_color="red").pack(anchor="w")
            return

        if not channels:
            ctk.CTkLabel(self._channel_frame, text="No channels found",
                         text_color="gray").pack(anchor="w")
            return

        for label in channels:
            var = ctk.BooleanVar(value=True)
            self._channel_vars[label] = var
            ctk.CTkCheckBox(self._channel_frame, text=label, variable=var).pack(
                side="left", padx=8)

    def _log_append(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _on_convert(self):
        lif_path = self._input_var.get().strip()
        output_dir = self._output_var.get().strip()

        if not lif_path or not Path(lif_path).is_file():
            messagebox.showerror("Error", "Please select a valid LIF file.")
            return
        if not output_dir:
            messagebox.showerror("Error", "Please specify an output directory.")
            return

        channels_filter = None
        if self._channel_vars:
            selected = {label for label, var in self._channel_vars.items() if var.get()}
            if not selected:
                messagebox.showerror("Error", "Select at least one channel.")
                return
            # Only apply filter if not all channels selected
            if len(selected) < len(self._channel_vars):
                channels_filter = selected

        self._convert_btn.configure(state="disabled", text="Converting...")
        self._progress_bar.set(0)
        self._progress_label.configure(text="0%")
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._log_append(f"> Input:  {lif_path}")
        self._log_append(f"> Output: {output_dir}")
        if self._dryrun_var.get():
            self._log_append("> Mode: dry-run (no files written)")

        self._worker = ConversionWorker(
            lif_path, output_dir,
            dry_run=self._dryrun_var.get(),
            channels_filter=channels_filter,
            msg_queue=self._queue,
        )
        self._worker.start()
        self._poll_queue()

    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, current, total, text = msg
                    frac = current / total if total else 0
                    self._progress_bar.set(frac)
                    self._progress_label.configure(text=f"{int(frac*100)}%")
                    self._log_append(f"> {text}")
                elif kind == "done":
                    result = msg[1]
                    self._on_done(success=True, result=result)
                    return
                elif kind == "error":
                    self._log_append(f"ERROR: {msg[1]}")
                    self._on_done(success=False)
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_done(self, success, result=None):
        self._convert_btn.configure(state="normal", text="Convert")
        if success:
            self._progress_bar.set(1)
            self._progress_label.configure(text="100%")

            if self._dryrun_var.get():
                self._log_append(f"\n> Dry-run complete.")
            else:
                self._log_append(f"\n> Conversion complete.")

                # Show validation and summary results
                if result:
                    val = result.get("validation", {})
                    if "error" not in val:
                        self._log_append(f"> Validation: {val['valid']}/{val['total']} series passed")
                        if val['failed'] > 0:
                            self._log_append(f"  WARNING: {val['failed']} series failed validation")
                        self._log_append(f"  Report: {val['report_path']}")

                    summ = result.get("summary", {})
                    if "error" not in summ:
                        self._log_append(f"> Metadata summary: {summ['csv_path']}")
                        self._log_append(f"  Total series: {summ['total_series']}")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
