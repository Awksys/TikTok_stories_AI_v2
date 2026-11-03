import whisperx
import string
import warnings
import re
import math
import gc
from statistics import median

from whisperx.audio import SAMPLE_RATE
from detect_title_timestamp import detect_title_end_timestamp
from transcription_archive import save_rejected_transcription

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


MAX_UNTRANSCRIBED_GAP_SECONDS = 15
MAX_WORD_DISPLAY_SECONDS = 15
GAP_EDGE_PADDING_SECONDS = 0.1
AUDIO_ANALYSIS_CHUNK_SECONDS = 0.1
MIN_ACTIVE_AUDIO_SECONDS = 0.4
MIN_CONTINUOUS_ACTIVE_SECONDS = 0.3
DEFAULT_ACTIVE_AUDIO_THRESHOLD_DBFS = -40.0
MIN_ACTIVE_AUDIO_THRESHOLD_DBFS = -45.0
MAX_ACTIVE_AUDIO_THRESHOLD_DBFS = -32.0
SPEECH_REFERENCE_DROP_DB = 18.0
VAD_ONSET = 0.4
VAD_OFFSET = 0.3
TRANSCRIPTION_CHUNK_SIZE_SECONDS = 15


class TranscriptionValidationError(RuntimeError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(issues))


def clean_word(word: str) -> str:
    keep_chars = "$@#%&"
    
    punctuation_to_remove = ''.join(ch for ch in string.punctuation if ch not in keep_chars)

    word = word.replace("\u200b", "").replace("\xa0", "")

    word = re.sub(r"[\u2013\u2014]", "", word)

    cleaned = re.sub(fr"^[{re.escape(punctuation_to_remove)}]+|[{re.escape(punctuation_to_remove)}]+$", "", word)

    return cleaned.strip()

def format_time(seconds: float) -> str:
    total_centiseconds = int(seconds * 100)
    h, remainder = divmod(total_centiseconds, 360000)
    m, remainder = divmod(remainder, 6000)
    s, cs = divmod(remainder, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def audio_dbfs(audio, start_seconds, end_seconds):
    start_sample = max(0, int(start_seconds * SAMPLE_RATE))
    end_sample = min(len(audio), int(end_seconds * SAMPLE_RATE))
    samples = audio[start_sample:end_sample]
    if len(samples) == 0:
        return float("-inf")

    rms = math.sqrt(float((samples.astype("float64") ** 2).mean()))
    if rms <= 0:
        return float("-inf")
    return 20 * math.log10(rms)


def estimate_active_audio_threshold(audio, word_timestamps):
    valid_words = [
        word for word in word_timestamps
        if word["end"] > word["start"]
    ]
    if not valid_words:
        return DEFAULT_ACTIVE_AUDIO_THRESHOLD_DBFS

    # Sampling keeps this inexpensive even for hour-long recordings.
    step = max(1, len(valid_words) // 200)
    speech_levels = []
    for word in valid_words[::step]:
        level = audio_dbfs(audio, word["start"], word["end"])
        if math.isfinite(level):
            speech_levels.append(level)

    if not speech_levels:
        return DEFAULT_ACTIVE_AUDIO_THRESHOLD_DBFS

    threshold = median(speech_levels) - SPEECH_REFERENCE_DROP_DB
    return min(
        MAX_ACTIVE_AUDIO_THRESHOLD_DBFS,
        max(MIN_ACTIVE_AUDIO_THRESHOLD_DBFS, threshold),
    )


def gap_contains_active_audio(audio, start_seconds, end_seconds, threshold_dbfs):
    analysis_start = start_seconds + GAP_EDGE_PADDING_SECONDS
    analysis_end = end_seconds - GAP_EDGE_PADDING_SECONDS
    if analysis_end <= analysis_start:
        return False, 0.0, 0.0

    active_seconds = 0.0
    current_active_run = 0.0
    longest_active_run = 0.0
    cursor = analysis_start

    while cursor < analysis_end:
        chunk_end = min(cursor + AUDIO_ANALYSIS_CHUNK_SECONDS, analysis_end)
        chunk_duration = chunk_end - cursor
        if audio_dbfs(audio, cursor, chunk_end) >= threshold_dbfs:
            active_seconds += chunk_duration
            current_active_run += chunk_duration
            longest_active_run = max(longest_active_run, current_active_run)
        else:
            current_active_run = 0.0
        cursor = chunk_end

    contains_active_audio = (
        active_seconds >= MIN_ACTIVE_AUDIO_SECONDS
        and longest_active_run >= MIN_CONTINUOUS_ACTIVE_SECONDS
    )
    return contains_active_audio, active_seconds, longest_active_run


def validate_transcription(audio, word_timestamps):
    if not word_timestamps:
        return ["no timestamped words were produced"]

    audio_duration = len(audio) / SAMPLE_RATE
    valid_words = sorted(
        (
            {
                "word": word["word"],
                "start": max(0.0, float(word["start"])),
                "end": min(audio_duration, float(word["end"])),
                "display_start": max(
                    0.0,
                    float(word.get("display_start", word["start"])),
                ),
                "display_end": min(
                    audio_duration,
                    float(word.get("display_end", word["end"])),
                ),
            }
            for word in word_timestamps
            if word["start"] is not None
            and word["end"] is not None
            and word["end"] > word["start"]
        ),
        key=lambda word: (word["start"], word["end"]),
    )
    if not valid_words:
        return ["no valid word timestamps were produced"]

    issues = []
    for word in valid_words:
        display_duration = word["display_end"] - word["display_start"]
        if display_duration > MAX_WORD_DISPLAY_SECONDS:
            issues.append(
                f'{word["display_start"]:.2f}s-{word["display_end"]:.2f}s: '
                f'word "{word["word"]}" remains displayed for '
                f"{display_duration:.2f}s"
            )

    first_word = valid_words[0]
    previous_word = first_word
    previous_end = first_word["end"]
    for word in valid_words[1:]:
        gap_duration = word["start"] - previous_end
        if gap_duration > MAX_UNTRANSCRIBED_GAP_SECONDS:
            issues.append(
                f"{previous_end:.2f}s-{word['start']:.2f}s: "
                f"{gap_duration:.2f}s between "
                f'"{previous_word["word"]}" and "{word["word"]}"'
            )
        if word["end"] >= previous_end:
            previous_end = word["end"]
            previous_word = word

    threshold_dbfs = estimate_active_audio_threshold(audio, valid_words)
    boundary_gaps = []
    if first_word["start"] > MAX_UNTRANSCRIBED_GAP_SECONDS:
        boundary_gaps.append(
            (0.0, first_word["start"], f'before first word "{first_word["word"]}"')
        )
    if audio_duration - previous_end > MAX_UNTRANSCRIBED_GAP_SECONDS:
        boundary_gaps.append(
            (previous_end, audio_duration, f'after last word "{previous_word["word"]}"')
        )

    for gap_start, gap_end, boundary_context in boundary_gaps:
        has_active_audio, active_seconds, longest_run = gap_contains_active_audio(
            audio,
            gap_start,
            gap_end,
            threshold_dbfs,
        )
        if has_active_audio:
            issues.append(
                f"{gap_start:.2f}s-{gap_end:.2f}s: "
                f"{gap_end - gap_start:.2f}s without subtitles, "
                f"including {active_seconds:.2f}s of active audio "
                f"({longest_run:.2f}s continuous), {boundary_context}"
            )

    if issues:
        print(
            f"Transcription validation failed "
            f"(boundary audio threshold: {threshold_dbfs:.1f} dBFS):"
        )
        for issue in issues:
            print(f"  - {issue}")
    else:
        print(
            f"Transcription validation passed: no subtitle gap longer than "
            f"{MAX_UNTRANSCRIBED_GAP_SECONDS:.1f}s and no word displayed "
            f"longer than {MAX_WORD_DISPLAY_SECONDS:.1f}s."
        )

    return issues

def transcribe_and_generate_ass(
        audio_path,
        output_ass="subtitles.ass",
        language="en",
        overlap=0.1,
        min_gap=0,
        device="cpu",
        model_name="medium.en",
        episode_title=None,
    ):
    
    print("Loading WhisperX model...")
    print(
        f"Transcription settings: model={model_name}, language={language}, "
        f"chunk_size={TRANSCRIPTION_CHUNK_SIZE_SECONDS}s, "
        f"vad_onset={VAD_ONSET}, vad_offset={VAD_OFFSET}."
    )

    model = whisperx.load_model(
        model_name,
        device=device,
        compute_type="int8",
        language=language,
        vad_options={
            "vad_onset": VAD_ONSET,
            "vad_offset": VAD_OFFSET,
            "chunk_size": TRANSCRIPTION_CHUNK_SIZE_SECONDS,
        },
    )

    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(
        audio,
        batch_size=4,
        chunk_size=TRANSCRIPTION_CHUNK_SIZE_SECONDS,
    )
    del model
    gc.collect()

    print("Word-level alignment...")
    model_a, metadata = whisperx.load_align_model(language_code=language, device=device)
    result_aligned = whisperx.align(
        result["segments"],
        model_a,
        metadata,
        audio,
        device,
        return_char_alignments=False
    )
    del model_a
    gc.collect()

    ass_content = """[Script Info]
Title: Auto-Subtitles TikTok Style
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,
ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,110,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,5,50,50,0,1

[Events]
Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    animation_effect = (
        r"{\an5"                      # center
        r"\t(0,110,\fscx90\fscy90)" # return to normal size
        r"\c&H0000FFFF&"               # texte jaune
        r"\3c&H000000&"                # contour noir
        r"\bord8"                     # outline thickness
        r"\be10"                       # flou du contour
        r"\3a&H40&"                     # transparence du contour
        r"\shad0"                       # ombre
        r"\fsp-5}"                     # slightly tighter letters
    )

    prev_end = 0.0
    word_timestamps = []
    all_sentences = []

    words = []
    skipped_words = 0
    audio_duration = len(audio) / SAMPLE_RATE
    for segment in result_aligned["segments"]:
        all_sentences.append({
            "sentence": segment["text"].strip(),
            "start": segment["start"],
            "end": segment["end"]
        })

        for word in segment.get("words", []):
            text = clean_word(word.get("word", ""))
            start = word.get("start")
            end = word.get("end")
            if not text:
                continue
            if (
                start is None or end is None
                or not math.isfinite(start) or not math.isfinite(end)
                or end < start or end <= 0 or start >= audio_duration
            ):
                skipped_words += 1
                continue

            words.append({
                "word": text,
                "start": max(0.0, float(start)),
                "end": min(audio_duration, float(end)),
            })

    title_end = (
        detect_title_end_timestamp(episode_title, all_sentences)
        if episode_title else None
    )

    # Look ahead across segment boundaries so overlap cannot hide the next word.
    words.sort(key=lambda word: (word["start"], word["end"]))
    for i, word in enumerate(words):
        start = max(word["start"], prev_end + min_gap)
        extended_end = min(word["end"] + overlap, audio_duration)
        if i < len(words) - 1:
            extended_end = min(extended_end, words[i + 1]["start"])
        # The title card and its subtitles must disappear at the same boundary.
        if title_end is not None and word["start"] < title_end:
            extended_end = min(extended_end, title_end)

        # ASS stores centiseconds; a shorter event would never be visible.
        if int(extended_end * 100) <= int(start * 100):
            skipped_words += 1
            continue

        prev_end = extended_end
        start_time = format_time(start)
        end_time = format_time(extended_end)
        animated_text = f"{animation_effect}{word['word']}"
        ass_content += f"Dialogue: 0,{start_time},{end_time},Default,,0,0,0,,{animated_text}\n"
        word_timestamps.append({
            **word,
            "display_start": start,
            "display_end": extended_end,
        })

    if skipped_words:
        print(f"Alignment warning: {skipped_words} words have no usable subtitle timing.")

    print("Validating transcription coverage...")
    validation_issues = validate_transcription(audio, word_timestamps)
    if validation_issues:
        save_rejected_transcription(
            audio_path,
            result,
            result_aligned,
            ass_content,
            validation_issues,
            title=episode_title,
            settings={
                "model_name": model_name,
                "language": language,
                "device": device,
                "compute_type": "int8",
                "batch_size": 4,
                "sample_rate": SAMPLE_RATE,
                "audio_duration_seconds": audio_duration,
                "chunk_size_seconds": TRANSCRIPTION_CHUNK_SIZE_SECONDS,
                "vad_onset": VAD_ONSET,
                "vad_offset": VAD_OFFSET,
                "max_untranscribed_gap_seconds": MAX_UNTRANSCRIBED_GAP_SECONDS,
                "max_word_display_seconds": MAX_WORD_DISPLAY_SECONDS,
                "gap_edge_padding_seconds": GAP_EDGE_PADDING_SECONDS,
                "audio_analysis_chunk_seconds": AUDIO_ANALYSIS_CHUNK_SECONDS,
                "min_active_audio_seconds": MIN_ACTIVE_AUDIO_SECONDS,
                "min_continuous_active_seconds": MIN_CONTINUOUS_ACTIVE_SECONDS,
                "default_active_audio_threshold_dbfs": DEFAULT_ACTIVE_AUDIO_THRESHOLD_DBFS,
                "min_active_audio_threshold_dbfs": MIN_ACTIVE_AUDIO_THRESHOLD_DBFS,
                "max_active_audio_threshold_dbfs": MAX_ACTIVE_AUDIO_THRESHOLD_DBFS,
                "speech_reference_drop_db": SPEECH_REFERENCE_DROP_DB,
                "subtitle_overlap_seconds": overlap,
                "subtitle_min_gap_seconds": min_gap,
                "skipped_word_count": skipped_words,
            },
        )
        raise TranscriptionValidationError(validation_issues)

    with open(output_ass, "w", encoding="utf-8") as f:
        f.write(ass_content)

    print(f"Subtitles generated: {output_ass}")

    return all_sentences



if __name__ == "__main__":
    print(transcribe_and_generate_ass(
        audio_path="trimmed_audio.mp3",
        output_ass="subtitles.ass",
    ))
