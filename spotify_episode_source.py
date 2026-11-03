import json
import pickle
import random
import re
import threading
import time
import warnings
from pathlib import Path
import subprocess

import chromedriver_autoinstaller
import soundcard as sc
import soundfile as sf
import undetected_chromedriver as uc
from pydub import AudioSegment
from soundcard.mediafoundation import SoundcardRuntimeWarning
from selenium.common.exceptions import TimeoutException, UnableToSetCookieException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


SPOTIFY_SHOW_URL = "https://open.spotify.com/show/2X13Nmuod85vAbI5hP99oY"
SPOTIFY_CHANNEL = "Requested Reads Reddit Stories"
COOKIES_PATH = Path("Cookies/open.spotify.com.pkl")
USED_EPISODES_PATH = Path("used_episodes_from_spotify.json")
PLAY_BUTTON_SELECTOR = 'button[data-testid="play-button"]'
LOAD_MORE_EPISODES_XPATH = (
    "//button[@role='checkbox' and "
    "(contains(normalize-space(.), 'Charger plus') or "
    "contains(normalize-space(.), 'Load more'))]"
)
MAX_EPISODE_LOAD_MORE_ATTEMPTS = 20
HEADLESS = True
MAX_RECORD_SECONDS = 60 * 60
RECORD_CHUNK_SECONDS = 0.25
SAMPLE_RATE = 48_000
SILENCE_THRESHOLD = 0.003
START_AUDIO_THRESHOLD = 0.02
START_TIMEOUT_SECONDS = 20
END_SILENCE_SECONDS = 0.1
FADE_OUT_SECONDS = 1
FADE_OUT_END_DB = -12
MIN_RECORD_SECONDS = 5
MIN_NON_SILENT_SECONDS = 2
NON_SILENT_THRESHOLD_DBFS = -38
AUDIO_DEVICE_ERROR_CODES = ("0x88890004", "0x100000001")

warnings.filterwarnings(
    "ignore",
    message="data discontinuity in recording",
    category=SoundcardRuntimeWarning,
)


class AudioRecordingError(RuntimeError):
    pass


def is_audio_device_error(error):
    current_error = error
    seen_errors = set()

    while current_error and id(current_error) not in seen_errors:
        seen_errors.add(id(current_error))
        message = str(current_error).lower()
        if any(code in message for code in AUDIO_DEVICE_ERROR_CODES):
            return True
        current_error = getattr(current_error, "__context__", None)

    return False


def load_cookies(driver):
    if not COOKIES_PATH.exists():
        return False

    with COOKIES_PATH.open("rb") as cookie_file:
        cookies = pickle.load(cookie_file)

    driver.delete_all_cookies()
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
        except UnableToSetCookieException:
            continue

    driver.refresh()
    time.sleep(2)
    return True


def save_cookies(driver):
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COOKIES_PATH.open("wb") as cookie_file:
        pickle.dump(driver.get_cookies(), cookie_file)


def load_used_titles():
    if not USED_EPISODES_PATH.exists():
        return set()

    with USED_EPISODES_PATH.open("r", encoding="utf-8") as used_file:
        return set(json.load(used_file))


def save_used_title(title):
    used_titles = load_used_titles()
    used_titles.add(title)
    with USED_EPISODES_PATH.open("w", encoding="utf-8") as used_file:
        json.dump(sorted(used_titles), used_file, indent=2, ensure_ascii=False)


def normalize_all_caps_words(title):
    def normalize_word(match):
        word = match.group(0)
        if sum(char.isalpha() for char in word) >= 2 and word.upper() == word:
            word = word.lower()
        # Contractions and possessives can contain either apostrophe style.
        word = re.sub(
            r"(['\u2019])(?:S|T|RE|VE|LL|D|M)\b",
            lambda suffix: suffix.group(0).lower(),
            word,
        )
        return re.sub(r"^i(?=$|['\u2019])", "I", word)

    title = re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", normalize_word, title)
    return re.sub(
        r"(^|[.!?]\s+)([\"'\u201c\u2018(\[]*)([a-z])",
        lambda match: match.group(1) + match.group(2) + match.group(3).upper(),
        title,
    )


def clean_title(label):
    title = " ".join(label.split())
    title = re.sub(r"^Lire\s*:?\s*", "", title, flags=re.IGNORECASE)

    suffix = f" par {SPOTIFY_CHANNEL}"
    if title.endswith(suffix):
        title = title[: -len(suffix)]

    return normalize_all_caps_words(title.strip())


def title_looks_truncated(title):
    closing_characters = '"\'\u201d\u2019)]'
    title_end = title.strip().rstrip(closing_characters)
    return not title_end or title_end[-1] not in ".!?\u2026"


def safe_filename(title):
    filename = re.sub(r'[<>:"/\\|?*]', "", title)
    filename = re.sub(r"\s+", " ", filename).strip()
    return filename[:140].rstrip(" .")


def parse_duration_seconds(text):
    normalized = " ".join(text.replace("\xa0", " ").split()).lower()
    hours = re.search(r"(\d+)\s*h", normalized)
    minutes = re.search(r"(\d+)\s*min", normalized)
    seconds = re.search(r"(\d+)\s*s", normalized)

    return (
        (int(hours.group(1)) * 3600 if hours else 0)
        + (int(minutes.group(1)) * 60 if minutes else 0)
        + (int(seconds.group(1)) if seconds else 0)
    )


def get_episode_duration_seconds(button):
    duration_text = button.parent.execute_script(
        """
        const button = arguments[0];
        const durationRegex = /(?:\\d+\\s*h\\s*)?(?:\\d+\\s*min\\s*)?(?:\\d+\\s*s)/i;

        let node = button;
        for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
            const spans = Array.from(node.querySelectorAll("span"));
            for (const span of spans) {
                const text = (span.innerText || span.textContent || "")
                    .replace(/\\u00a0/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();

                if (
                    text
                    && !text.toLowerCase().includes("restante")
                    && durationRegex.test(text)
                ) {
                    return text;
                }
            }
        }

        return null;
        """,
        button,
    )

    if not duration_text:
        return None

    return parse_duration_seconds(duration_text) or None


def find_play_buttons(driver):
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, PLAY_BUTTON_SELECTOR))
        )
    except TimeoutException:
        print("Spotify: no play buttons found after 20s; the episode page may not have loaded.")
        try:
            print(f"Spotify page: {driver.title} ({driver.current_url})")
        except WebDriverException:
            pass
        return []
    return driver.find_elements(By.CSS_SELECTOR, PLAY_BUTTON_SELECTOR)


def list_available_episodes(driver):
    used_titles = {normalize_all_caps_words(title) for title in load_used_titles()}

    for attempt in range(MAX_EPISODE_LOAD_MORE_ATTEMPTS + 1):
        buttons = find_play_buttons(driver)
        if not buttons:
            return []

        episodes = []
        seen_titles = set()
        excluded = {
            "used": 0,
            "duplicate": 0,
            "truncated_title": 0,
            "no_title": 0,
            "no_duration": 0,
        }

        for button in buttons:
            title = clean_title(button.get_attribute("aria-label") or "")
            if not title:
                excluded["no_title"] += 1
                continue
            if title_looks_truncated(title):
                excluded["truncated_title"] += 1
                continue
            if title in seen_titles:
                excluded["duplicate"] += 1
                continue
            if title in used_titles:
                excluded["used"] += 1
                continue

            duration_seconds = get_episode_duration_seconds(button)
            if not duration_seconds:
                excluded["no_duration"] += 1
                continue

            episodes.append({
                "title": title,
                "button": button,
                "duration_seconds": duration_seconds,
            })
            seen_titles.add(title)

        print(
            f"Spotify scan: {len(buttons)} play buttons, {len(episodes)} eligible episodes; "
            f"excluded: used={excluded['used']}, duplicate={excluded['duplicate']}, "
            f"truncated_title={excluded['truncated_title']}, "
            f"no_title={excluded['no_title']}, no_duration={excluded['no_duration']}."
        )
        if episodes:
            return episodes
        if attempt >= MAX_EPISODE_LOAD_MORE_ATTEMPTS:
            print("Spotify: reached the limit for loading more episodes.")
            return []

        load_more_buttons = driver.find_elements(By.XPATH, LOAD_MORE_EPISODES_XPATH)
        if not load_more_buttons:
            print("Spotify: no eligible episodes and no load-more control found.")
            return []

        previous_labels = {button.get_attribute("aria-label") for button in buttons}
        try:
            print("Spotify: loading more episodes...")
            load_more_buttons[0].click()
            WebDriverWait(driver, 20).until(
                lambda current_driver: any(
                    button.get_attribute("aria-label") not in previous_labels
                    for button in current_driver.find_elements(By.CSS_SELECTOR, PLAY_BUTTON_SELECTOR)
                )
            )
        except WebDriverException as error:
            print(f"Spotify: could not load additional episodes ({type(error).__name__}).")
            return []

    return []


def press_escape(driver):
    time.sleep(1)
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    time.sleep(0.5)


def click_episode(driver, button):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    time.sleep(0.5)
    button.click()
    press_escape(driver)


def audio_rms(audio):
    return float((audio ** 2).mean() ** 0.5)


def _record_system_audio_once(wav_path, seconds_to_record, start_audio, wait_for_end_silence=True):
    speaker = sc.default_speaker()
    if speaker is None:
        raise RuntimeError("No audio output device found.")

    loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    if loopback is None:
        raise RuntimeError("Loopback audio device not found.")

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    seconds_to_record = min(seconds_to_record, MAX_RECORD_SECONDS)
    chunk_frames = int(RECORD_CHUNK_SECONDS * SAMPLE_RATE)

    start_audio_error = []
    start_audio_thread = None
    recorder_context = loopback.recorder(samplerate=SAMPLE_RATE)
    recorder = None
    recording_error = None

    def run_start_audio():
        try:
            start_audio()
        except Exception as error:
            start_audio_error.append(error)

    try:
        recorder = recorder_context.__enter__()
        start_audio_thread = threading.Thread(target=run_start_audio)
        start_audio_thread.start()
        start_wait_time = time.monotonic()

        while True:
            first_chunk = recorder.record(numframes=chunk_frames)
            if start_audio_error:
                raise start_audio_error[0]

            if audio_rms(first_chunk) >= START_AUDIO_THRESHOLD:
                break

            if time.monotonic() - start_wait_time >= START_TIMEOUT_SECONDS:
                break

        with sf.SoundFile(
            wav_path,
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=first_chunk.shape[1],
        ) as audio_file:
            audio_file.write(first_chunk)
            start_time = time.monotonic()
            end_silence_duration = 0

            while True:
                if start_audio_error:
                    raise start_audio_error[0]

                audio = recorder.record(numframes=chunk_frames)
                elapsed = time.monotonic() - start_time

                if elapsed < seconds_to_record:
                    audio_file.write(audio)
                    continue

                if not wait_for_end_silence:
                    break

                audio_file.write(audio)
                if audio_rms(audio) < SILENCE_THRESHOLD:
                    end_silence_duration += RECORD_CHUNK_SECONDS
                    if end_silence_duration >= END_SILENCE_SECONDS:
                        break
                    continue

                end_silence_duration = 0

    except Exception as error:
        recording_error = error
        raise
    finally:
        if start_audio_thread is not None:
            start_audio_thread.join(timeout=5)

        if recorder is not None:
            try:
                recorder_context.__exit__(None, None, None)
            except Exception as close_error:
                if recording_error is None:
                    raise
                print(f"Ignoring recorder close error after recording failure: {close_error}")


def record_system_audio(wav_path, seconds_to_record, start_audio, wait_for_end_silence=True):
    try:
        _record_system_audio_once(
            wav_path,
            seconds_to_record,
            start_audio,
            wait_for_end_silence=wait_for_end_silence,
        )
    except Exception as error:
        if is_audio_device_error(error):
            wav_path.unlink(missing_ok=True)
            raise AudioRecordingError(
                "Windows audio loopback device was reset or invalidated during recording."
            ) from error
        raise


def convert_wav_to_mp3(wav_path, mp3_path):
    audio = AudioSegment.from_wav(wav_path)
    fade_duration_ms = min(FADE_OUT_SECONDS * 1000, len(audio))
    if fade_duration_ms > 0:
        head = audio[:-fade_duration_ms]
        tail = audio[-fade_duration_ms:]
        faded_tail = tail.fade(to_gain=FADE_OUT_END_DB, start=0, duration=fade_duration_ms)
        audio = head + faded_tail
    audio.export(mp3_path, format="mp3", bitrate="320k")
    wav_path.unlink(missing_ok=True)


def audio_has_enough_content(audio_path):
    audio = AudioSegment.from_file(audio_path)
    duration_seconds = len(audio) / 1000

    if duration_seconds < MIN_RECORD_SECONDS:
        print(f"Recorded audio rejected: too short ({duration_seconds:.2f}s).")
        return False

    non_silent_ms = 0
    chunk_ms = 250
    for start_ms in range(0, len(audio), chunk_ms):
        if audio[start_ms:start_ms + chunk_ms].dBFS > NON_SILENT_THRESHOLD_DBFS:
            non_silent_ms += chunk_ms

    non_silent_seconds = non_silent_ms / 1000
    if non_silent_seconds < MIN_NON_SILENT_SECONDS:
        print(
            f"Recorded audio rejected: only {non_silent_seconds:.2f}s "
            f"above {NON_SILENT_THRESHOLD_DBFS} dBFS."
        )
        return False

    return True


def start_driver():
    driver = uc.Chrome(
        options=uc.ChromeOptions(),
        driver_executable_path=chromedriver_autoinstaller.install(),
        headless=HEADLESS,
        use_subprocess=False,
    )
    driver.maximize_window()
    return driver


def close_driver(driver):
    try:
        driver.close()
    except Exception:
        pass

    try:
        driver.quit()
    except Exception:
        pass


def record_random_spotify_episode(
    output_file="Downloads/audio.mp3",
    record_seconds_override=None,
    max_seconds=None,
    mark_as_used=True,
):
    output_file = Path(output_file)
    wav_file = output_file.with_suffix(".wav")
    driver = start_driver()

    try:
        driver.get(SPOTIFY_SHOW_URL)
        time.sleep(2)

        if not load_cookies(driver):
            print("No Spotify cookie found.")
            print("Log in / accept cookies in Chrome, then press Enter here.")
            input()
            save_cookies(driver)
            driver.get(SPOTIFY_SHOW_URL)
            time.sleep(2)

        episodes = list_available_episodes(driver)
        if not episodes:
            return None

        episode = random.choice(episodes)
        print(f"Selected Spotify episode: {episode['title']}")

        seconds_to_record = (
            record_seconds_override
            if record_seconds_override is not None
            else episode["duration_seconds"]
        )
        reached_max_seconds = False

        if max_seconds is not None:
            if max_seconds <= 0:
                raise ValueError("max_seconds must be greater than 0.")

            reached_max_seconds = seconds_to_record > max_seconds
            seconds_to_record = min(seconds_to_record, max_seconds)

        print(f"Recording Spotify audio for up to {seconds_to_record:.0f}s")
        try:
            record_system_audio(
                wav_file,
                seconds_to_record,
                lambda: click_episode(driver, episode["button"]),
                wait_for_end_silence=record_seconds_override is None and not reached_max_seconds,
            )
        except AudioRecordingError as error:
            print(f"Spotify recording failed: {error}")
            print("Episode was not marked as used; the workflow can try again.")
            output_file.unlink(missing_ok=True)
            wav_file.unlink(missing_ok=True)
            return None

        convert_wav_to_mp3(wav_file, output_file)
        if not audio_has_enough_content(output_file):
            output_file.unlink(missing_ok=True)
            wav_file.unlink(missing_ok=True)
            return None

        if mark_as_used:
            save_used_title(episode["title"])
        save_cookies(driver)

        return {
            "id": safe_filename(episode["title"]),
            "title": episode["title"],
            "url": SPOTIFY_SHOW_URL,
            "channel": SPOTIFY_SHOW_URL,
            "channel_name": SPOTIFY_CHANNEL,
            "audio_path": str(output_file),
            "duration_seconds": episode["duration_seconds"],
        }

    finally:
        close_driver(driver)

def kill_chrome_processes():
    for process_name in ("chrome.exe", "chromedriver.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", process_name, "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(2)

if __name__ == "__main__":
    kill_chrome_processes()
    print(record_random_spotify_episode(
        output_file="Downloads/test_audio.mp3",
        record_seconds_override=10,
        mark_as_used=False,
    ))
