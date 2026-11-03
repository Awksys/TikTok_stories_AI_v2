import re
import string


MAX_TITLE_WORD_ERRORS = 1


def clean_word(word):
    word = word.replace("\u200b", "").replace("\xa0", " ")
    word = word.lower().strip()
    word = word.strip(string.punctuation + "\u201c\u201d\u2018\u2019")
    return word


def words_from_text(text):
    words = []
    for word in re.split(r"\s+", text):
        cleaned = clean_word(word)
        if cleaned:
            words.append(cleaned)
    return words


def transcript_words_with_timestamps(full_sentences_v0):
    transcript_words = []

    for segment in full_sentences_v0:
        timestamp = segment["end"]
        for word in words_from_text(segment["sentence"]):
            transcript_words.append((word, timestamp))

    return transcript_words


def match_title_with_error_budget(title_words, transcript_words, max_errors):
    states = set()

    for transcript_word, timestamp in transcript_words:
        states.add((0, 0))
        next_states = set()

        for title_index, errors in states:
            if title_index > 0 and errors < max_errors:
                # Extra word in transcript.
                next_states.add((title_index, errors + 1))

            remaining_errors = max_errors - errors
            for skipped_title_words in range(remaining_errors + 1):
                candidate_title_index = title_index + skipped_title_words
                candidate_errors = errors + skipped_title_words

                if candidate_title_index >= len(title_words):
                    break

                word_error = 0 if transcript_word == title_words[candidate_title_index] else 1
                total_errors = candidate_errors + word_error

                if total_errors > max_errors:
                    continue

                next_title_index = candidate_title_index + 1
                if next_title_index == len(title_words):
                    return timestamp

                next_states.add((next_title_index, total_errors))

        states = next_states

    return None


def detect_title_end_timestamp(title, full_sentences_v0):
    title_words = words_from_text(title)
    if not title_words:
        return None

    transcript_words = transcript_words_with_timestamps(full_sentences_v0)

    exact_match = match_title_with_error_budget(
        title_words,
        transcript_words,
        max_errors=0,
    )
    if exact_match is not None:
        return exact_match

    tolerant_match = match_title_with_error_budget(
        title_words,
        transcript_words,
        max_errors=MAX_TITLE_WORD_ERRORS,
    )
    if tolerant_match is not None:
        print(f"Title matched with up to {MAX_TITLE_WORD_ERRORS} word errors.")

    return tolerant_match


if __name__ == "__main__":
    title = "My husband had a vasectomy, and now everyone is calling me a CHEATER because I got pregnant?"
    full_sentences_v0 = [
        {"sentence": "My husband had a vasectomy,", "start": 0.0, "end": 2.1},
        {"sentence": "and now everyone is calling me a CHEATER", "start": 2.1, "end": 5.8},
        {"sentence": "because I got pregnant?", "start": 5.8, "end": 7.2},
    ]

    print(detect_title_end_timestamp(title, full_sentences_v0))
