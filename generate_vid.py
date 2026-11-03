import subprocess
import time
import os
import glob
import re
import string
import textwrap
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydub import AudioSegment

from get_subtitles import TranscriptionValidationError, transcribe_and_generate_ass
from openai_image import main_image_gen
from detect_title_timestamp import detect_title_end_timestamp

import asyncio


POP_SOUND_GAIN_DB = -20
POP_SOUND_SILENCE_THRESHOLD_DBFS = -35
POP_SOUND_SILENCE_CHUNK_MS = 5
POP_SOUND_PREROLL_MS = 15
ILLUSTRATION_CROP_WIDTH = 720
ILLUSTRATION_EXPECTED_WIDTH = 1024
ILLUSTRATION_MOTION_SPEED = 25
ILLUSTRATION_MAX_DURATION = (
    (ILLUSTRATION_EXPECTED_WIDTH - ILLUSTRATION_CROP_WIDTH)
    / ILLUSTRATION_MOTION_SPEED
)


def clear_folder(folder_path):
    files = glob.glob(os.path.join(folder_path, "*"))
    for f in files:
        if os.path.isfile(f):
            os.remove(f)
    print(f"All files in '{folder_path}' have been deleted.")

def get_audio_duration(audio_file):
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-hide_banner",
        "-loglevel", "quiet",
        audio_file
    ], stdout=subprocess.PIPE, text=True)
    return float(result.stdout.strip())


def find_pop_sound_file(sounds_dir="Sounds"):
    exact_match = os.path.join(sounds_dir, "freesound_community-pop-39222.mp3")
    if os.path.exists(exact_match):
        return exact_match

    matches = sorted(glob.glob(os.path.join(sounds_dir, "*pop*.mp3")))
    return matches[0] if matches else None


def detect_leading_silence_seconds(audio_path):
    audio = AudioSegment.from_file(audio_path)

    trim_ms = 0
    while trim_ms < len(audio):
        chunk = audio[trim_ms:trim_ms + POP_SOUND_SILENCE_CHUNK_MS]
        if chunk.dBFS > POP_SOUND_SILENCE_THRESHOLD_DBFS:
            break
        trim_ms += POP_SOUND_SILENCE_CHUNK_MS

    if trim_ms >= len(audio):
        return 0

    return max(0, trim_ms - POP_SOUND_PREROLL_MS) / 1000


def build_audio_with_intro_pop(audio_file, pop_sound_file, output_file="temp_story_with_pop.wav"):
    if not pop_sound_file:
        return audio_file

    story_audio = AudioSegment.from_file(audio_file)
    pop_audio = AudioSegment.from_file(pop_sound_file)
    pop_trim_start_ms = int(detect_leading_silence_seconds(pop_sound_file) * 1000)

    if pop_trim_start_ms > 0:
        pop_audio = pop_audio[pop_trim_start_ms:]

    pop_audio = (
        pop_audio
        .set_frame_rate(story_audio.frame_rate)
        .set_channels(story_audio.channels)
        .apply_gain(POP_SOUND_GAIN_DB)
    )

    mixed_audio = story_audio.overlay(pop_audio, position=0)
    mixed_audio.export(output_file, format="wav")
    print(f"Pop sound overlaid at 0.00s: {pop_sound_file} (trimmed {pop_trim_start_ms / 1000:.3f}s)")
    return output_file


def generate_rounded_rect_png(filename="rounded_rect.png", width=400, height=200, radius=30):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, width, height], radius, fill=(255, 255, 255, 255))
    img.save(filename)
    return filename


def generate_shadow_from_rect(rect_file, shadow_file="shadow_rect.png", shadow_offset=50, blur_radius=10, radius=30):
    with Image.open(rect_file) as rect:
        width, height = rect.size
    shadow_width = width + shadow_offset * 2
    shadow_height = height + shadow_offset * 2
    shadow_img = Image.new("RGBA", (shadow_width, shadow_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow_img)
    draw.rounded_rectangle(
        [shadow_offset, shadow_offset, width + shadow_offset, height + shadow_offset],
        radius=radius,
        fill=(0, 0, 0, 200)
    )
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(blur_radius))
    shadow_img.save(shadow_file)
    return shadow_file


def wrap_text_to_width(text, font, max_width):
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)

    def width(s):
        bbox = draw.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0]

    lines = []
    for paragraph in text.splitlines():
        words = paragraph.split(" ")
        if not words:
            lines.append("")
            continue
        cur = ""
        for w in words:
            candidate = w if cur == "" else cur + " " + w
            if width(candidate) <= max_width:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                if width(w) <= max_width:
                    cur = w
                else:
                    part = ""
                    for ch in w:
                        test = part + ch
                        if width(test) <= max_width:
                            part = test
                        else:
                            if part:
                                lines.append(part)
                            part = ch
                    cur = part
        if cur:
            lines.append(cur)
    return lines


def generate_video(audio_file, ass_file, output_file, title_duration, title, account_index, videos_dir="Videos_clips", pop=False):
    print("Generating final video...")

    concat_file = "concat.txt"
    merged_video = "temp_background.mp4"
    VIDEO_W, VIDEO_H = 720, 1280
    fps = 30

    def extract_number(f):
        match = re.search(r'(\d+)\.mp4$', f)
        return int(match.group(1)) if match else -1

    video_files = sorted(
        [os.path.join(videos_dir, f) for f in os.listdir(videos_dir) if f.endswith(".mp4")],
        key=lambda x: extract_number(os.path.basename(x))
    )
    if not video_files:
        raise FileNotFoundError(f"No videos found in {videos_dir} directory")

    with open(concat_file, "w", encoding="utf-8") as f:
        for vf in video_files:
            f.write(f"file '{os.path.abspath(vf)}'\n")

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-r", f"{fps}",
        "-fflags", "+genpts",
        "-an",
        "-hide_banner",
        "-loglevel", "quiet",
        merged_video, "-y"
    ], check=True)

    rect_w, rect_h = 520, 240
    radius = 30
    combined_file = "rect_shadow_combined.png"

    try:
        font = ImageFont.truetype("Fonts/Roboto-ExtraBold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()

    margin_top = 80
    margin_bottom = 54
    margin_left = 16
    margin_right = 16
    line_spacing = 8

    max_width = rect_w - margin_left - margin_right
    lines = wrap_text_to_width(title, font, max_width)

    bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), "Hg", font=font)
    line_height = bbox[3] - bbox[1]

    total_text_h = len(lines) * line_height + (len(lines) - 1) * line_spacing
    rect_h_final = total_text_h + margin_top + margin_bottom

    img_rect = Image.new("RGBA", (rect_w, rect_h_final), (0, 0, 0, 0))
    draw_rect = ImageDraw.Draw(img_rect)
    draw_rect.rounded_rectangle([0, 0, rect_w, rect_h_final], radius, fill=(255, 255, 255, 255))
    rect_file = "rounded_rect.png"
    img_rect.save(rect_file)
    shadow_file = generate_shadow_from_rect(rect_file, radius=radius)

    rect_x = (VIDEO_W - rect_w) // 2
    rect_y = (VIDEO_H - rect_h_final) // 2

    combined = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))

    with Image.open(shadow_file) as shadow:
        shadow_x = (VIDEO_W - shadow.width) // 2
        shadow_y = (VIDEO_H - shadow.height) // 2
        combined.alpha_composite(shadow, (shadow_x, shadow_y))

    with Image.open(rect_file) as rect:
        combined.alpha_composite(rect, (rect_x, rect_y))

    
    image1_path = f"Illustrator/Final/1_{account_index}.png"
    image2_path = "Illustrator/Final/2.png"
    image3_path = "Illustrator/Final/3.png"
    image4_path = "Illustrator/Final/4.png"

    image1_ratio = 0.5
    image2_ratio = 0.25
    image3_ratio = 0.15
    image4_ratio = 0.06

    if os.path.exists(image1_path):
        with Image.open(image1_path) as image1:
            image1 = image1.convert("RGBA")
            new_w = int(rect_w * image1_ratio)
            new_h = int(image1.height * (new_w / image1.width))
            image1 = image1.resize((new_w, new_h), Image.LANCZOS)
            x1 = rect_x + 15
            y1 = rect_y 
            combined.alpha_composite(image1, (x1, y1))

    if os.path.exists(image2_path):
        with Image.open(image2_path) as image2:
            image2 = image2.convert("RGBA")
            new_w = int(rect_w * image2_ratio)
            new_h = int(image2.height * (new_w / image2.width))
            image2 = image2.resize((new_w, new_h), Image.LANCZOS)
            x2 = rect_x + 15
            y2 = rect_y + rect_h_final - image2.height - 15
            combined.alpha_composite(image2, (x2, y2))

    if os.path.exists(image3_path):
        with Image.open(image3_path) as image3:
            image3 = image3.convert("RGBA")
            new_w = int(rect_w * image3_ratio)
            new_h = int(image3.height * (new_w / image3.width))
            image3 = image3.resize((new_w, new_h), Image.LANCZOS)
            x3 = rect_x + rect_w - image3.width - 15
            y3 = rect_y + rect_h_final - image3.height - 15
            combined.alpha_composite(image3, (x3, y3))

    if os.path.exists(image4_path):
        with Image.open(image4_path) as image4:
            image4 = image4.convert("RGBA")
            new_w = int(rect_w * image4_ratio)
            new_h = int(image4.height * (new_w / image4.width))
            image4 = image4.resize((new_w, new_h), Image.LANCZOS)
            x4 = rect_x + rect_w - image4.width - 20
            y4 = rect_y + 10
            combined.alpha_composite(image4, (x4, y4))


    draw = ImageDraw.Draw(combined)
    current_y = rect_y + margin_top
    for line in lines:
        text_x = rect_x + margin_left
        draw.text((text_x, current_y), line, font=font, fill=(0, 0, 0, 255))
        current_y += line_height + line_spacing

    combined.save(combined_file)

    min_zoom = 1.04
    max_zoom = 1.06
    rect_duration = float(title_duration)
    animation = f"({min_zoom}+{max_zoom-1}*(1-(cos(t*PI/{rect_duration})*exp(-85*t))))"

    filter_complex = (
        rf"[0:v]ass={ass_file}[subbed];"
        rf"[1:v]format=rgba,scale=iw*{animation}:ih*{animation}:eval=frame,fps={fps}[rect];"
        rf"[subbed][rect]overlay=x='(W-w)/2':y='(H-h)/2':eval=frame:enable='between(t,0,{rect_duration})'[vout_final]"
    )

    pop_sound_file = find_pop_sound_file() if pop else None
    mixed_audio_file = build_audio_with_intro_pop(audio_file, pop_sound_file)

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i", merged_video,
        "-loop", "1",
        "-i", combined_file,
        "-i", mixed_audio_file,
    ]

    ffmpeg_cmd += [
        "-filter_complex",
        filter_complex,
        "-map", "[vout_final]",
        "-map", "2:a",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", f"{fps}",
        "-shortest",
        "-movflags", "+faststart",
        "-hide_banner",
        "-loglevel", "quiet",
        output_file
    ]

    subprocess.run(ffmpeg_cmd, check=True)

    temp_files = [merged_video, concat_file, rect_file, shadow_file, combined_file, ass_file]
    if mixed_audio_file != audio_file:
        temp_files.append(mixed_audio_file)

    for f in temp_files:
        if os.path.exists(f):
            os.remove(f)

    print(f"Final video created: {output_file}")



def clean_word(word: str) -> str:
    return word.strip(string.punctuation)



def trim_audio(audio_path, max_minutes):
    max_ms = max_minutes * 60 * 1000
    audio = AudioSegment.from_file(audio_path)

    if len(audio) > max_ms:
        trimmed_audio = audio[:max_ms]
        trimmed_path = "trimmed_audio.mp3"
        trimmed_audio.export(trimmed_path, format="mp3")
        return trimmed_path
    
    return audio_path


def numeric_sort_key(filename):
    match = re.search(r"\d+", filename)
    return int(match.group()) if match else float("inf")

def resize_images(image_folder="AI_Images", resized_folder="resized"):
    os.makedirs(resized_folder, exist_ok=True)

    images = sorted([f for f in os.listdir(image_folder) if f.lower().endswith((".png", ".jpg", ".jpeg"))], key=numeric_sort_key)

    for img_name in images:
        input_path = os.path.join(image_folder, img_name)
        output_path = os.path.join(resized_folder, img_name)

        cmd = [
            "ffmpeg",
            "-y",
            "-i", input_path,
            "-vf", "scale=-1:1280,eq=brightness=0:contrast=0.8:saturation=0.4",
            "-hide_banner",
            "-loglevel", "quiet",
            output_path
        ]

        subprocess.run(cmd, check=True)
        #print(f"Image resized: {output_path}")


import random

def create_videos_from_image(full_sentences, resized_folder="resized", output_folder="Videos_clips"):
    os.makedirs(output_folder, exist_ok=True)

    fps = 30
    crop_width = ILLUSTRATION_CROP_WIDTH
    speed = ILLUSTRATION_MOTION_SPEED

    images = sorted([f for f in os.listdir(resized_folder) if f.lower().endswith((".png", ".jpg", ".jpeg"))], key=numeric_sort_key)

    for i, img_name in enumerate(images):

        img_path = os.path.join(resized_folder, img_name)
        output_path = os.path.join(output_folder, f"{i+1}.mp4")

        print(img_path, output_path)

        start = full_sentences[i]["start"]
        end = full_sentences[i]["end"]
        duration = max(end - start, 0.1)

        direction = random.choice(["left", "right"])

        with Image.open(img_path) as image:
            image_width = image.width

        if image_width < crop_width:
            raise ValueError(f"{img_path} is too narrow: {image_width}px < {crop_width}px crop width.")

        center_x = (image_width - crop_width) / 2
        max_x = image_width - crop_width
        max_duration = max_x / speed if speed > 0 else duration

        if duration <= max_duration + 0.001:
            amplitude = (speed * duration) / 2
            if direction == "left":
                x_expr = f"({center_x:.3f}+{amplitude:.3f})-t*{speed}"
            else:
                x_expr = f"({center_x:.3f}-{amplitude:.3f})+t*{speed}"
        else:
            print(
                f"Illustration clip {i + 1} is {duration:.2f}s, above "
                f"{max_duration:.2f}s max for constant motion; using bounded fallback."
            )
            if direction == "left":
                x_expr = f"{max_x:.3f}-abs(mod(t*{speed}, {2 * max_x:.3f})-{max_x:.3f})"
            else:
                x_expr = f"abs(mod(t*{speed}, {2 * max_x:.3f})-{max_x:.3f})"

        vf_filter = f"[0:v]crop={crop_width}:1280:x='{x_expr}':y=0"


        cmd = [
            "ffmpeg",
            "-y",
            "-loop", "1",
            "-i", img_path,
            "-t", str(duration),
            "-filter_complex", vf_filter,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-hide_banner",
            "-loglevel", "quiet",
            output_path
        ]

        subprocess.run(cmd, check=True)
        #print(f"Video generated: {output_path}")


def merge_short_sentences(full_sentences, title_dur, min_duration=4, max_gap=0.5, max_duration=ILLUSTRATION_MAX_DURATION):

    merged_sentences = []
    new_full_sentences = []

    buffer_sentence = ""
    buffer_start = None
    buffer_end = None

    for i, entry in enumerate(full_sentences):
        sentence = entry["sentence"]
        start = entry["start"]
        end = entry["end"]

        if title_dur is not None:
            if abs(start - title_dur) < max_gap:
                start = title_dur
            if abs(end - title_dur) < max_gap:
                end = title_dur

        if buffer_sentence and max_duration and (end - buffer_start) > max_duration:
            merged_sentences.append(buffer_sentence.strip())
            new_full_sentences.append({
                "sentence": buffer_sentence.strip(),
                "start": buffer_start,
                "end": buffer_end
            })
            buffer_sentence = sentence
            buffer_start = start
            buffer_end = end
        elif not buffer_sentence:
            buffer_sentence = sentence
            buffer_start = start
            buffer_end = end
        else:
            buffer_sentence += " " + sentence
            buffer_end = end

        if (buffer_end - buffer_start) >= min_duration:
            merged_sentences.append(buffer_sentence.strip())
            new_full_sentences.append({
                "sentence": buffer_sentence.strip(),
                "start": buffer_start,
                "end": buffer_end
            })
            buffer_sentence = ""
            buffer_start = None
            buffer_end = None

    if buffer_sentence:
        if merged_sentences:
            merged_sentences[-1] += " " + buffer_sentence.strip()
            new_full_sentences[-1]["sentence"] += " " + buffer_sentence.strip()
            new_full_sentences[-1]["end"] = buffer_end
        else:
            merged_sentences.append(buffer_sentence.strip())
            new_full_sentences.append({
                "sentence": buffer_sentence.strip(),
                "start": buffer_start,
                "end": buffer_end
            })

    for i in range(len(new_full_sentences) - 1):
        end_current = new_full_sentences[i]["end"]
        start_next = new_full_sentences[i + 1]["start"]
        mid = (end_current + start_next) / 2
        new_full_sentences[i]["end"] = mid
        new_full_sentences[i + 1]["start"] = mid

    return merged_sentences, new_full_sentences


def get_title(full_sentences, timecode):
    title_parts = []

    for sentence in full_sentences:
        if sentence["end"] < timecode + 0.1:
            title_parts.append(sentence["sentence"])
        else:
            break

    return " ".join(title_parts).strip()
    

def main_generate(audio, title, account_index, max_minutes=60, pop=False):
    start_time = time.time()
    subs = "subtitles.ass"
    output = "Outputs/final_output.mp4"

    audio = trim_audio(audio, max_minutes)

    try:
        full_sentences_v0 = transcribe_and_generate_ass(
            audio_path=audio, output_ass=subs, episode_title=title,
        )
    except TranscriptionValidationError as error:
        if os.path.exists(subs):
            os.remove(subs)
        print(f"Video generation cancelled: invalid transcription ({error}).")
        print("The Spotify workflow will select another episode.")
        return None

    print(full_sentences_v0[:5])

    title_duration = detect_title_end_timestamp(title, full_sentences_v0)
    if title_duration is None:
        os.remove(subs)
        print("Title end timestamp not found.")
        return None

    sentences_list, full_sentences = merge_short_sentences(full_sentences_v0, title_duration)

    clear_folder("AI_Images")
    clear_folder("resized")
    clear_folder("Videos_clips")

    video_full_duration = get_audio_duration(audio)
    full_sentences[0]["start"] = 0
    full_sentences[-1]["end"] = video_full_duration

    print(full_sentences[:5])
    
    print(title, title_duration)

    if title_duration > 1:
    
        asyncio.run(main_image_gen(sentences_list))
        
        resize_images()
        create_videos_from_image(full_sentences)

        generate_video(audio, subs, output, title_duration, title, account_index, pop=pop)

        end_time = time.time()
        elapsed = end_time - start_time
        print(f"Total runtime: {elapsed:.2f} seconds ({elapsed/60:.2f} minutes)")

        clear_folder("AI_Images")
        clear_folder("resized")
        clear_folder("Videos_clips")
        
        return output
    
    else:
        os.remove(subs)
        return None


if __name__ == "__main__":
    main_generate(
        "Downloads/audio.mp3",
        "My husband had a vasectomy, and now everyone is calling me a CHEATER because I got pregnant?",
        2,
        max_minutes=1,
    )


