import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

with (
    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-used"}),
    patch("dotenv.load_dotenv"),
    patch("openai.AsyncOpenAI"),
):
    import openai_image as images


class ImageFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        logger = patch.object(images, "log")
        logger.start()
        self.addCleanup(logger.stop)

    def make_image(self, index, color):
        path = self.directory / f"{index}.png"
        Image.new("RGB", (1024, 1024), color).save(path, "PNG")
        return path

    def assert_complete(self, count):
        self.assertTrue(all(images.image_exists(i, self.directory) for i in range(1, count + 1)))

    def test_missing_image_copies_previous_without_changing_following_image(self):
        first = self.make_image(1, "red").read_bytes()
        third = self.make_image(3, "blue").read_bytes()
        images.fill_missing_images(self.directory, ["first", "second", "third"])
        self.assert_complete(3)
        self.assertEqual((self.directory / "2.png").read_bytes(), first)
        self.assertEqual((self.directory / "3.png").read_bytes(), third)

    def test_consecutive_missing_images_keep_their_numbered_slots(self):
        first = self.make_image(1, "red").read_bytes()
        fourth = self.make_image(4, "blue").read_bytes()
        images.fill_missing_images(self.directory, ["a", "b", "c", "d"])
        self.assert_complete(4)
        self.assertEqual((self.directory / "2.png").read_bytes(), first)
        self.assertEqual((self.directory / "3.png").read_bytes(), first)
        self.assertEqual((self.directory / "4.png").read_bytes(), fourth)

    def test_first_missing_images_use_the_next_available_image(self):
        third = self.make_image(3, "blue").read_bytes()
        images.fill_missing_images(self.directory, ["a", "b", "c"])
        self.assert_complete(3)
        self.assertEqual((self.directory / "1.png").read_bytes(), third)
        self.assertEqual((self.directory / "2.png").read_bytes(), third)

    def test_all_generation_failures_still_produce_usable_images(self):
        images.fill_missing_images(self.directory, ["a", "b", "c"])
        self.assert_complete(3)
        with Image.open(self.directory / "1.png") as image:
            self.assertEqual(image.size, (1024, 1024))

    def test_corrupted_image_is_replaced(self):
        first = self.make_image(1, "red").read_bytes()
        (self.directory / "2.png").write_bytes(b"not an image")
        images.fill_missing_images(self.directory, ["a", "b"])
        self.assert_complete(2)
        self.assertEqual((self.directory / "2.png").read_bytes(), first)

    def test_clip_numbers_and_durations_do_not_shift(self):
        self.make_image(1, "red")
        self.make_image(3, "blue")
        images.fill_missing_images(self.directory, ["a", "b", "c"])
        generator = importlib.import_module("generate_vid")
        sentences = [
            {"start": 0, "end": 1},
            {"start": 1, "end": 3},
            {"start": 3, "end": 6},
        ]
        with patch.object(generator.subprocess, "run") as render, patch("builtins.print"):
            generator.create_videos_from_image(
                sentences, str(self.directory), str(self.directory / "clips"),
            )
        commands = [call.args[0] for call in render.call_args_list]
        self.assertEqual([Path(cmd[cmd.index("-i") + 1]).name for cmd in commands], ["1.png", "2.png", "3.png"])
        self.assertEqual([float(cmd[cmd.index("-t") + 1]) for cmd in commands], [1, 2, 3])


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_failure_stops_after_two_attempts(self):
        with (
            patch.object(images, "image_exists", return_value=False),
            patch.object(images, "call_image_generation", new_callable=AsyncMock) as generate,
            patch.object(images, "compute_backoff", return_value=0),
            patch.object(images.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(images, "log") as logger,
        ):
            generate.side_effect = asyncio.TimeoutError()
            await asyncio.wait_for(images.generate_image(2, "scene", asyncio.Semaphore(1)), timeout=1)
        self.assertEqual(generate.await_count, 2)
        logger.assert_any_call("Final failure for image 2")

    async def test_prompt_rejection_gets_one_safe_retry_then_stops(self):
        response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
        error = images.BadRequestError("rejected", response=response, body={})
        with (
            patch.object(images, "image_exists", return_value=False),
            patch.object(images, "call_image_generation", new_callable=AsyncMock) as generate,
            patch.object(images, "make_prompt_safe", new_callable=AsyncMock, return_value="safe scene") as rewrite,
            patch.object(images, "log"),
        ):
            generate.side_effect = error
            await asyncio.wait_for(images.generate_image(2, "scene", asyncio.Semaphore(1)), timeout=1)
        self.assertEqual(generate.await_count, 2)
        rewrite.assert_awaited_once()

    async def test_main_fills_failed_task_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            image_dir = Path(directory)

            async def generate(index, prompt, semaphore):
                if index == 2:
                    raise RuntimeError("generation failed")
                Image.new("RGB", (32, 32), "red" if index == 1 else "blue").save(image_dir / f"{index}.png")

            with (
                patch.object(images, "IMAGE_DIR", image_dir),
                patch.object(images, "generate_image", side_effect=generate),
                patch.object(images, "log"),
            ):
                await images.main_image_gen(["a", "b", "c"])
            self.assertEqual((image_dir / "2.png").read_bytes(), (image_dir / "1.png").read_bytes())
            with Image.open(image_dir / "3.png") as image:
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))


if __name__ == "__main__":
    unittest.main()
