import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import get_subtitles as subtitles
import transcription_archive as archives


class TranscriptionArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.audio = self.directory / "Downloads" / "audio.wav"
        self.audio.parent.mkdir()
        self.write_audio(1)
        self.root = self.directory / "diagnostics"
        self.transcription = {"segments": [{"text": "My sister left.", "start": 0, "end": 1}]}
        self.alignment = {"segments": [{"words": [{"word": "sister", "start": np.float32(0.1)}]}]}

    def write_audio(self, duration):
        samples = np.full(int(duration * subtitles.SAMPLE_RATE), 0.1, dtype=np.float32)
        with wave.open(str(self.audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(subtitles.SAMPLE_RATE)
            output.writeframes((samples * 32767).astype("<i2").tobytes())
        return samples

    def save(self, **overrides):
        arguments = {
            "audio_path": self.audio,
            "transcription": self.transcription,
            "alignment": self.alignment,
            "ass_content": "[Events]\n",
            "issues": ["19.00s between subtitle words"],
            "title": "My sister left.",
            "settings": {"chunk_size_seconds": 15},
            "archive_dir": self.root,
        }
        arguments.update(overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            return archives.save_rejected_transcription(**arguments)

    def test_archive_keeps_audio_json_subtitles_and_rejection_details(self):
        original = self.audio.read_bytes()
        saved = self.save()
        self.assertIsNotNone(saved)
        self.assertEqual((saved / "audio.wav").read_bytes(), original)
        self.assertEqual(json.loads((saved / "transcription.json").read_text()), self.transcription)
        aligned = json.loads((saved / "alignment.json").read_text())
        self.assertAlmostEqual(aligned["segments"][0]["words"][0]["start"], 0.1)
        self.assertEqual((saved / "subtitles.ass").read_text(), "[Events]\n")
        metadata = json.loads((saved / "metadata.json").read_text())
        self.assertEqual(metadata["title"], "My sister left.")
        self.assertEqual(metadata["issues"], ["19.00s between subtitle words"])
        self.assertEqual(metadata["settings"], {"chunk_size_seconds": 15})
        self.audio.unlink()
        self.assertEqual((saved / "audio.wav").read_bytes(), original)

    def test_only_five_latest_archives_are_retained(self):
        self.root.mkdir()
        readme = self.root / "README.md"
        readme.write_text("Do not remove this file.")
        unrelated = self.root / "manual_notes"
        unrelated.mkdir()
        similar_name = self.root / "20200101_000000_000000_12345678"
        similar_name.mkdir()
        (similar_name / "metadata.json").write_text('{"kind": "other"}')
        saved = [self.save(title=f"Example {i}.") for i in range(7)]
        self.assertTrue(all(folder is not None for folder in saved))
        self.assertTrue(all(not folder.exists() for folder in saved[:2]))
        self.assertTrue(all(folder.is_dir() for folder in saved[2:]))
        self.assertTrue(readme.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(similar_name.exists())

    def test_archive_with_manually_added_file_is_preserved(self):
        saved = self.save(keep=1)
        notes = saved / "notes.txt"
        notes.write_text("Keep for investigation.")
        newest = self.save(keep=1)
        self.assertTrue(notes.exists())
        self.assertTrue(newest.exists())

    def test_missing_audio_does_not_raise_or_create_an_archive(self):
        self.audio.unlink()
        self.assertIsNone(self.save())
        self.assertFalse(self.root.exists())

    def test_unserializable_data_cleans_partial_archive_without_raising(self):
        self.assertIsNone(self.save(alignment={"invalid": object()}))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unwritable_destination_does_not_change_existing_file(self):
        self.root.write_text("Existing file.")
        self.assertIsNone(self.save())
        self.assertEqual(self.root.read_text(), "Existing file.")

    def test_retention_failure_keeps_the_new_archive(self):
        with patch.object(archives, "_prune_old_archives", side_effect=OSError("access denied")):
            saved = self.save()
        self.assertTrue((saved / "audio.wav").is_file())
        self.assertTrue((saved / "metadata.json").is_file())

    def test_rejected_pipeline_archives_once_before_original_audio_is_removed(self):
        audio = self.write_audio(32)
        original = self.audio.read_bytes()
        model = Mock()
        raw = {"segments": [{"text": "My sister. She left.", "start": 0, "end": 21}]}
        model.transcribe.return_value = raw
        aligned = {"segments": [
            {"text": "My sister.", "start": 0, "end": 1, "words": [
                {"word": "My", "start": 0, "end": 0.3},
                {"word": "sister.", "start": 0.4, "end": 1},
            ]},
            {"text": "She left.", "start": 20, "end": 21, "words": [
                {"word": "She", "start": 20, "end": 20.3},
                {"word": "left.", "start": 20.4, "end": 21},
            ]},
        ]}
        with (
            patch.object(archives, "TRANSCRIPTION_DIAGNOSTICS_DIR", self.root),
            patch.object(subtitles.whisperx, "load_model", return_value=model),
            patch.object(subtitles.whisperx, "load_audio", return_value=audio),
            patch.object(subtitles.whisperx, "load_align_model", return_value=(Mock(), {})),
            patch.object(subtitles.whisperx, "align", return_value=aligned),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(subtitles.TranscriptionValidationError):
                subtitles.transcribe_and_generate_ass(
                    str(self.audio), str(self.directory / "subtitles.ass"),
                    episode_title="My sister. She left.",
                )
        model.transcribe.assert_called_once()
        saved = list(self.root.iterdir())
        self.assertEqual(len(saved), 1)
        folder = saved[0]
        metadata = json.loads((folder / "metadata.json").read_text())
        self.assertEqual(metadata["title"], "My sister. She left.")
        self.assertTrue(any('between "sister" and "She"' in issue for issue in metadata["issues"]))
        self.assertEqual(metadata["settings"]["vad_onset"], subtitles.VAD_ONSET)
        self.assertEqual(json.loads((folder / "transcription.json").read_text()), raw)
        self.assertEqual(json.loads((folder / "alignment.json").read_text()), aligned)
        self.assertIn("Dialogue:", (folder / "subtitles.ass").read_text())
        self.assertFalse((self.directory / "subtitles.ass").exists())
        self.audio.unlink()
        self.assertEqual((folder / "audio.wav").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
