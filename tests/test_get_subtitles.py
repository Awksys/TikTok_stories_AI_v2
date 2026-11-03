import copy
import contextlib
import importlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

import get_subtitles as subtitles
from detect_title_timestamp import detect_title_end_timestamp


def ass_seconds(value):
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class SubtitlePipelineTests(unittest.TestCase):
    def run_pipeline(self, aligned_segments, duration=4, episode_title=None):
        audio = np.full(int(duration * subtitles.SAMPLE_RATE), 0.1, dtype=np.float32)
        model = Mock()
        transcript = {"segments": [{"start": 0, "end": duration, "text": "Example."}]}
        model.transcribe.return_value = transcript
        aligner = object()
        metadata = {"language": "en"}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "subtitles.ass"
            with (
                patch.object(subtitles.whisperx, "load_model", return_value=model) as load_model,
                patch.object(subtitles.whisperx, "load_audio", return_value=audio),
                patch.object(subtitles.whisperx, "load_align_model", return_value=(aligner, metadata)),
                patch.object(subtitles.whisperx, "align", return_value={"segments": aligned_segments}) as align,
                patch.object(subtitles, "save_rejected_transcription") as archive,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                sentences = subtitles.transcribe_and_generate_ass(
                    "recording.mp3", str(output), episode_title=episode_title,
                )
            content = output.read_text(encoding="utf-8")

        model.transcribe.assert_called_once_with(audio, batch_size=4, chunk_size=15)
        archive.assert_not_called()
        self.assertIs(align.call_args.args[3], audio)
        self.assertEqual(load_model.call_args.kwargs["vad_options"], {
            "vad_onset": 0.4, "vad_offset": 0.3, "chunk_size": 15,
        })
        events = []
        for line in content.splitlines():
            if line.startswith("Dialogue:"):
                fields = line.split(",", 9)
                events.append((ass_seconds(fields[1]), ass_seconds(fields[2]), fields[9]))
        self.assertTrue(events)
        for start, end, _ in events:
            self.assertLess(start, end)
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(end, duration)
        for previous, following in zip(events, events[1:]):
            self.assertLessEqual(previous[1], following[0])
        return sentences, events

    def test_fast_words_remain_visible_at_segment_boundary(self):
        segments = [
            {"text": "My", "start": 0, "end": 0.98, "words": [
                {"word": "My", "start": 0, "end": 0.98},
            ]},
            {"text": "sister called.", "start": 1, "end": 1.5, "words": [
                {"word": "sister", "start": 1, "end": 1.04},
                {"word": "called.", "start": 1.05, "end": 1.5},
            ]},
        ]
        original = copy.deepcopy(segments)
        sentences, events = self.run_pipeline(segments)
        self.assertEqual(len(events), 3)
        self.assertIn("sister", events[1][2])
        self.assertEqual(events[0][1], 1)
        self.assertEqual(segments, original)
        self.assertEqual(sentences, [
            {"sentence": "My", "start": 0, "end": 0.98},
            {"sentence": "sister called.", "start": 1, "end": 1.5},
        ])

    def test_missing_and_invalid_timestamps_do_not_break_ass(self):
        self.run_pipeline([{
            "text": "A valid sentence.", "start": 0, "end": 4,
            "words": [
                {"word": "A", "start": 0, "end": 0.4},
                {"word": "missing"},
                {"word": "nan", "start": float("nan"), "end": 2},
                {"word": "infinite", "start": 1, "end": float("inf")},
                {"word": "reversed", "start": 2, "end": 1},
                {"word": "outside", "start": 8, "end": 9},
                {"word": "sentence.", "start": 3, "end": 4},
            ],
        }])

    def test_title_subtitles_end_before_the_title_card_disappears(self):
        for story_start in (0.98, 1.05, 1.3):
            with self.subTest(story_start=story_start):
                segments = [
                    {"text": "My title.", "start": 0, "end": 0.98, "words": [
                        {"word": "My", "start": 0, "end": 0.3},
                        {"word": "title.", "start": 0.35, "end": 0.98},
                    ]},
                    {"text": "Story begins.", "start": story_start, "end": 4, "words": [
                        {"word": "Story", "start": story_start, "end": story_start + 0.08},
                        {"word": "begins.", "start": story_start + 0.1, "end": 4},
                    ]},
                ]
                _, original_events = self.run_pipeline(segments)
                sentences, events = self.run_pipeline(segments, episode_title="My title.")
                title_end = detect_title_end_timestamp("My title.", sentences)

                self.assertEqual(len(events), 4)
                self.assertLessEqual(events[1][1], title_end)
                self.assertEqual(events[0], original_events[0])
                self.assertEqual(events[2:], original_events[2:])

    def test_events_shorter_than_ass_precision_are_not_written(self):
        _, events = self.run_pipeline([{
            "text": "A tiny word.", "start": 0, "end": 4,
            "words": [
                {"word": "A", "start": 0, "end": 0.7},
                {"word": "tiny", "start": 1.001, "end": 1.003},
                {"word": "word.", "start": 1.004, "end": 4},
            ],
        }])
        self.assertEqual(len(events), 2)

    def test_short_event_survives_centisecond_conversion(self):
        _, events = self.run_pipeline([{
            "text": "A short word.", "start": 0, "end": 6,
            "words": [
                {"word": "A", "start": 0, "end": 0.7},
                {"word": "short", "start": 5.169, "end": 5.17},
                {"word": "word.", "start": 5.171, "end": 6},
            ],
        }], duration=6)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1][:2], (5.16, 5.17))

    def test_failed_validation_transcribes_once_and_writes_no_ass(self):
        audio = np.ones(subtitles.SAMPLE_RATE, dtype=np.float32)
        model = Mock()
        model.transcribe.return_value = {"segments": []}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rejected.ass"
            with (
                patch.object(subtitles.whisperx, "load_model", return_value=model),
                patch.object(subtitles.whisperx, "load_audio", return_value=audio),
                patch.object(subtitles.whisperx, "load_align_model", return_value=(Mock(), {})),
                patch.object(subtitles.whisperx, "align", return_value={"segments": []}),
                patch.object(subtitles, "save_rejected_transcription", return_value=None) as archive,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(subtitles.TranscriptionValidationError):
                    subtitles.transcribe_and_generate_ass("recording.mp3", str(output))
            self.assertFalse(output.exists())
        model.transcribe.assert_called_once()
        archive.assert_called_once()

    def test_existing_gap_and_display_limits_are_preserved(self):
        audio = np.ones(34 * subtitles.SAMPLE_RATE, dtype=np.float32)
        with contextlib.redirect_stdout(io.StringIO()):
            issues = subtitles.validate_transcription(audio, [
                {"word": "before", "start": 0, "end": 1},
                {"word": "after", "start": 17, "end": 18},
                {"word": "stuck", "start": 18, "end": 34},
            ])
        self.assertTrue(any('between "before" and "after"' in issue for issue in issues))
        self.assertTrue(any('word "stuck"' in issue for issue in issues))
        self.assertEqual(subtitles.MAX_UNTRANSCRIBED_GAP_SECONDS, 15)
        self.assertEqual(subtitles.MAX_WORD_DISPLAY_SECONDS, 15)

    def test_generation_stops_before_images_after_rejected_transcription(self):
        image_module = types.ModuleType("openai_image")
        image_module.main_image_gen = AsyncMock()
        with patch.dict(sys.modules, {"openai_image": image_module}):
            generator = importlib.import_module("generate_vid")
        with (
            patch.object(generator, "trim_audio", return_value="audio.mp3"),
            patch.object(generator, "transcribe_and_generate_ass", side_effect=subtitles.TranscriptionValidationError(["gap"])),
            patch.object(generator, "clear_folder") as clear,
            patch.object(generator, "main_image_gen", new_callable=AsyncMock) as images,
            patch.object(generator, "generate_video") as render,
            patch.object(generator.os.path, "exists", return_value=False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertIsNone(generator.main_generate("audio.mp3", "My title.", 1))
        clear.assert_not_called()
        images.assert_not_called()
        render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
