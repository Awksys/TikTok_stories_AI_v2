import os
import random
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import chromedriver_autoinstaller

from generate_vid import main_generate
from spotify_episode_source import record_random_spotify_episode
from tiktok_uploader_from_spotify import close_driver, start_driver, upload_video_from_spotify


base_path = Path(__file__).parent


def clean_folder(folder):
    if not os.path.exists(folder):
        return

    for item in os.listdir(folder):
        path = os.path.join(folder, item)
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
        else:
            shutil.rmtree(path)


def kill_chrome_processes():
    for process_name in ("chrome.exe", "chromedriver.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", process_name, "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(2)


def main_video(post_count_target, wait_time, account_index, driver=None, pop=True):
    posted_count = 0
    clean_folder("Downloads")

    try:
        while posted_count < post_count_target:
            episode = record_random_spotify_episode("Downloads/audio.mp3", max_seconds=3600)

            if not episode:
                print("No Spotify episode available.")
                time.sleep(120)
                continue

            print(f"Podcast: {episode['channel_name']}")
            print(f"Title  : {episode['title']}")

            output = main_generate(
                episode["audio_path"],
                episode["title"],
                account_index,
                pop=pop,
            )

            if output:
                if not driver:
                    chromedriver_path = chromedriver_autoinstaller.install(True)
                    driver = start_driver(
                        "https://www.tiktok.com/tiktokstudio/upload?from=creator_center",
                        f"Cookies/cookies_tiktok_{account_index}.pkl",
                        "TikTok",
                        chromedriver_path,
                    )

                success = upload_video_from_spotify(driver, output, episode, base_path, account_index)

                error_dir = "upload_errors"
                os.makedirs(error_dir, exist_ok=True)

                if success:
                    posted_count += 1
                    time.sleep(random.randint(wait_time, wait_time + 1200))
                else:
                    time.sleep(random.randint(60, 180))
                    success = upload_video_from_spotify(driver, output, episode, base_path, account_index)
                    if not success:
                        new_file_path = f"upload_errors/{episode['id']}.mp4"
                        shutil.move(output, new_file_path)
                        print(f"Error with the upload, file moved to {new_file_path}")
                    else:
                        print(f"Upload succeeded on retry for episode {episode['id']}")

            clean_folder("Downloads")

    finally:
        if driver:
            close_driver(driver)


TARGET_HOUR = 16
KILL_CHROME_ON_START = True


def run_daily():

    has_run_today = False

    print("Starting Spotify workflow...")
    if KILL_CHROME_ON_START:
        kill_chrome_processes()

    while True:
        now = datetime.now()

        if now.hour >= TARGET_HOUR and not has_run_today:
            print("Starting tasks...")
            main_video(post_count_target=1, wait_time=0, account_index=1)
            main_video(post_count_target=1, wait_time=0, account_index=2)
            main_video(post_count_target=1, wait_time=7200, account_index=1)
            has_run_today = True
            print("Tasks completed")

        if now.hour == 8:
            has_run_today = False

        time.sleep(300)


if __name__ == "__main__":
    run_daily()
