import os
import random
import base64
import asyncio
from pathlib import Path
from io import BytesIO
from shutil import copyfile

from PIL import Image
from openai import (
    AsyncOpenAI,
    BadRequestError,
    RateLimitError,
    APIConnectionError,
    InternalServerError,
    APIStatusError,
)

# =========================================================
# CONFIG
# =========================================================

# IMPORTANT :
# Mets ta clé dans une variable d'environnement :
# Windows PowerShell :
#   $env:OPENAI_API_KEY="sk-...."
# Ou via un fichier .env si tu préfères.

from dotenv import load_dotenv

env_path = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=env_path)

API_KEY = os.environ.get("OPENAI_API_KEY")

if not API_KEY:
    raise ValueError(
        "OPENAI_API_KEY introuvable. "
        "Définis la variable d'environnement avant de lancer le script."
    )

client = AsyncOpenAI(
    api_key=API_KEY,
    timeout=300.0,   # sécurité globale côté SDK
    max_retries=1    # on garde peu de retries internes, car on gère nous-mêmes
)

IMAGE_MODEL = "gpt-image-2"
REWRITE_MODEL = "gpt-4o-mini"

IMAGE_DIR = Path("AI_Images")
LOG_FILE = Path("image_generation.log")

CONCURRENCY = 6
MAX_IMAGE_ATTEMPTS = 2
IMAGE_REQUEST_TIMEOUT = 300       # 5 min max par tentative image
REWRITE_REQUEST_TIMEOUT = 60      # 1 min max pour réécriture safe

QUALITY = "low"
MODERATION = "low"
SIZE = "1024x1024"

SWITCH_TO_SAFE_AFTER = 3          # si erreurs techniques répétées, on tente prompt safe
REWRITE_AGAIN_EVERY = 5           # si même le safe prompt ne passe pas, on réécrit encore
MAX_BACKOFF = 60                  # délai max entre retries

sentences = [
    "My husband told me he'd only consider having children with me if I quit my job and learn to be like his mother.",
    "After seven years of marriage, Daniel dropped this bomb during our anniversary dinner. His mother spent 40 years serving his father breakfast in bed, ironing his underwear, and asking permission to visit her own sister.",
    "That's the woman I needed to become if I wanted to be worthy of carrying his children.",
    "I'm a software architect making twice his salary, but apparently that made me less of a woman in his eyes.",
    "The thing is, when we met, Daniel loved my ambition. He bragged to his friends about dating a woman who led tech teams and spoke at conferences.",
    "He'd bring me coffee during late night coding sessions, said my passion was attractive. We split household chores equally.",
    "He even told his mother once that he was lucky to find someone who challenged him intellectually.",
    "That was before his dad retired and his parents started visiting more often.",
    "Suddenly Daniel noticed things. Why didn't I pack his lunch like his mother did for his father?",
    "Why was our house not spotless when his mother kept theirs immaculate? Why did I order takeout when his mother cooked three meals from scratch daily? His mother Nancy,"
]

COMMON_STYLE = (
    "ultra realistic cinematic photograph, vertical 9:16 composition, "
    "immersive first-person storytelling, subject performing actions described in the scene, "
    "detailed environment and background elements matching the story, "
    "natural lighting, rich color contrast, soft depth of field, 35mm lens aesthetic, "
    "authentic human emotion, lifelike skin texture, realistic interaction with objects or surroundings, "
    "dynamic composition capturing the story moment, consistent characters, no text or captions"
)

SAFE_STYLE = (
    "The scene must be safe for all audiences and social media: "
    "no violence, blood, weapons, sexual content, illegal or harmful actions. "
    "Any conflicts should be shown in a safe, abstract, or emotionally suggestive way."
)

# =========================================================
# UTILS
# =========================================================

def log(message: str):
    print(message)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(message + "\n")


def ensure_dirs():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def get_image_path(i: int) -> Path:
    return IMAGE_DIR / f"{i}.png"


def image_exists(i: int, image_dir=None) -> bool:
    path = get_image_path(i) if image_dir is None else Path(image_dir) / f"{i}.png"
    try:
        with Image.open(path) as image:
            image.load()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def build_full_prompt(scene_prompt: str) -> str:
    return f"{COMMON_STYLE}. {SAFE_STYLE}. Scene: {scene_prompt}"


def compute_backoff(attempt: int) -> float:
    # backoff exponentiel doux + un peu d'aléatoire
    return min(MAX_BACKOFF, (2 ** min(attempt, 5)) + random.uniform(0.5, 2.0))


def is_prompt_policy_error(error: Exception) -> bool:
    """
    Détecte les erreurs qui semblent venir du prompt / des règles de sécurité.
    """
    text = str(error).lower()

    keywords = [
        "safety",
        "policy",
        "content policy",
        "moderation",
        "violence",
        "sexual",
        "not allowed",
        "disallowed",
        "invalid prompt",
        "refused",
        "rejected"
    ]

    if isinstance(error, BadRequestError):
        return True

    return any(k in text for k in keywords)


def local_soft_safe_rewrite(original_prompt: str) -> str:
    """
    Fallback local si la réécriture par LLM échoue.
    On garde l'idée générale, mais on adoucit très fortement.
    """
    return (
        "A safe, family-friendly cinematic scene that illustrates the emotional idea of the following story moment: "
        f"{original_prompt}. "
        "Show only subtle tension through facial expressions, posture, distance, and surrounding objects. "
        "No explicit confrontation, no harmful content, no graphic elements, no illegal or sexual content. "
        "Preserve the relationship dynamics, setting, and emotional meaning in a gentle and non-graphic way."
    )


# =========================================================
# OPENAI HELPERS
# =========================================================

async def make_prompt_safe(original_prompt: str, sem: asyncio.Semaphore, stronger: bool = False) -> str:
    """
    Réécrit un prompt en version plus safe.
    stronger=False : réécriture normale
    stronger=True  : réécriture encore plus prudente
    """
    if stronger:
        system_message = (
            "You are an assistant that rewrites prompts for image generation in an especially safe way. "
            "Preserve the core meaning, setting, genders, and emotional intent, but make the scene clearly acceptable "
            "for family-friendly image generation. "
            "Avoid any explicit conflict, domination, coercion, sexuality, nudity, graphic suffering, harmful actions, "
            "or disturbing details. Use implication, body language, distance, objects, and atmosphere instead. "
            "If needed, convert the moment into a gentle, emotionally expressive, symbolic or neutral version. "
            "Return only the rewritten prompt."
        )
    else:
        system_message = (
            "You are an assistant that rewrites text prompts for safe image generation. "
            "Keep the original scene, characters' genders, setting, emotions, and main action. "
            "Soften violent, sexual, illegal, or sensitive content into a non-graphic, family-friendly equivalent. "
            "Suggest tension or conflict through expressions, body language, shadows, distance, or symbolic details "
            "instead of explicit content. "
            "Do not include blood, gore, visible wounds, nudity, explicit violence, or graphic suffering. "
            "Return only the rewritten prompt."
        )

    user_message = f"Make this prompt safe for image generation: '{original_prompt}'"

    async with sem:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=REWRITE_MODEL,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.7,
                max_tokens=200,
            ),
            timeout=REWRITE_REQUEST_TIMEOUT,
        )

    safe_prompt = response.choices[0].message.content.strip()
    return safe_prompt


async def call_image_generation(full_prompt: str, sem: asyncio.Semaphore):
    """
    Appel OpenAI image avec timeout par tentative.
    """
    async with sem:
        result = await asyncio.wait_for(
            client.images.generate(
                model=IMAGE_MODEL,
                prompt=full_prompt,
                size=SIZE,
                quality=QUALITY,
                moderation=MODERATION,
            ),
            timeout=IMAGE_REQUEST_TIMEOUT,
        )
    return result


# =========================================================
# IMAGE GENERATION CORE
# =========================================================

async def generate_image(i: int, original_prompt: str, sem: asyncio.Semaphore):
    """
    Genere une image, puis laisse le remplacement combler un echec final.
    Safe fallback conservé.
    """
    path = get_image_path(i)

    if image_exists(i):
        log(f"[Image {i}] Déjà existante, on skip -> {path}")
        return

    current_prompt = original_prompt.strip()
    safe_prompt_used = False
    attempt = 0

    while attempt < MAX_IMAGE_ATTEMPTS:
        attempt += 1
        full_prompt = build_full_prompt(current_prompt)

        try:
            log(f"[Image {i}] Tentative {attempt} | safe_prompt_used={safe_prompt_used}")

            result = await call_image_generation(full_prompt, sem)

            if not result.data or not result.data[0].b64_json:
                raise RuntimeError("Réponse image vide ou invalide.")

            image_bytes = base64.b64decode(result.data[0].b64_json)

            # Vérification du fichier image
            with Image.open(BytesIO(image_bytes)) as img:
                img.load()
                width, height = img.size

            if (width, height) != (1024, 1024):
                log(f"[Image {i}] Avertissement: dimensions reçues {width}x{height} au lieu de 1024x1024")

            with open(path, "wb") as f:
                f.write(image_bytes)

            usage = getattr(result, "usage", None)
            if usage is not None:
                log(f"[Image {i}] Usage: {usage}")

            log(f"[Image {i}] Succès -> {path}")
            return

        except asyncio.TimeoutError:
            log(f"[Image {i}] Timeout après {IMAGE_REQUEST_TIMEOUT}s (tentative {attempt})")

        except BadRequestError as e:
            log(f"[Image {i}] BadRequestError (tentative {attempt}) : {e}")
            if attempt >= MAX_IMAGE_ATTEMPTS:
                break

            # Erreur très probablement liée au prompt -> fallback safe immédiat
            try:
                if not safe_prompt_used:
                    log(f"[Image {i}] Passage immédiat au safe prompt...")
                    current_prompt = await make_prompt_safe(current_prompt, sem, stronger=False)
                    safe_prompt_used = True
                    log(f"[Image {i}] Nouveau safe prompt : {current_prompt}")
                else:
                    log(f"[Image {i}] Le safe prompt a aussi échoué -> réécriture safe plus stricte...")
                    current_prompt = await make_prompt_safe(current_prompt, sem, stronger=True)
                    log(f"[Image {i}] Nouveau safe prompt renforcé : {current_prompt}")
                continue
            except Exception as rewrite_error:
                log(f"[Image {i}] Échec de la réécriture safe : {rewrite_error}")
                current_prompt = local_soft_safe_rewrite(current_prompt)
                safe_prompt_used = True
                log(f"[Image {i}] Fallback local appliqué.")
                continue

        except RateLimitError as e:
            log(f"[Image {i}] Rate limit (tentative {attempt}) : {e}")

        except APIConnectionError as e:
            log(f"[Image {i}] Erreur de connexion API (tentative {attempt}) : {e}")

        except InternalServerError as e:
            log(f"[Image {i}] Erreur serveur OpenAI (tentative {attempt}) : {e}")

        except APIStatusError as e:
            log(f"[Image {i}] APIStatusError (tentative {attempt}) : {e}")
            if attempt >= MAX_IMAGE_ATTEMPTS:
                break

            # Si ça ressemble à un refus de prompt, on tente le fallback safe
            if is_prompt_policy_error(e):
                try:
                    if not safe_prompt_used:
                        log(f"[Image {i}] L'erreur semble liée au prompt -> safe fallback.")
                        current_prompt = await make_prompt_safe(current_prompt, sem, stronger=False)
                        safe_prompt_used = True
                    else:
                        current_prompt = await make_prompt_safe(current_prompt, sem, stronger=True)
                    continue
                except Exception as rewrite_error:
                    log(f"[Image {i}] Échec réécriture après APIStatusError : {rewrite_error}")
                    current_prompt = local_soft_safe_rewrite(current_prompt)
                    safe_prompt_used = True
                    continue

        except Exception as e:
            log(f"[Image {i}] Erreur inattendue (tentative {attempt}) : {repr(e)}")

        if attempt >= MAX_IMAGE_ATTEMPTS:
            break

        # Si on a beaucoup d'échecs techniques, on tente aussi une version safe
        if attempt >= SWITCH_TO_SAFE_AFTER and not safe_prompt_used:
            try:
                log(f"[Image {i}] Plusieurs échecs -> tentative de safe fallback préventif...")
                current_prompt = await make_prompt_safe(current_prompt, sem, stronger=False)
                safe_prompt_used = True
                log(f"[Image {i}] Safe prompt obtenu : {current_prompt}")
                continue
            except Exception as rewrite_error:
                log(f"[Image {i}] Impossible d'obtenir un safe prompt : {rewrite_error}")
                current_prompt = local_soft_safe_rewrite(current_prompt)
                safe_prompt_used = True
                continue

        # Si même le safe prompt a du mal, on le réécrit périodiquement
        if safe_prompt_used and attempt % REWRITE_AGAIN_EVERY == 0:
            try:
                log(f"[Image {i}] Réécriture safe supplémentaire après plusieurs échecs...")
                current_prompt = await make_prompt_safe(current_prompt, sem, stronger=True)
                log(f"[Image {i}] Safe prompt renforcé : {current_prompt}")
                continue
            except Exception as rewrite_error:
                log(f"[Image {i}] Réécriture safe supplémentaire impossible : {rewrite_error}")
                current_prompt = local_soft_safe_rewrite(current_prompt)

        delay = compute_backoff(attempt)
        log(f"[Image {i}] Nouvelle tentative dans {delay:.1f}s...")
        await asyncio.sleep(delay)

    log(f"Final failure for image {i}")


# =========================================================
# VERIFICATION
# =========================================================

def fill_missing_images(image_dir, full_sentences):
    image_dir = Path(image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    total_count = len(full_sentences)
    for i in range(1, total_count + 1):
        if image_exists(i, image_dir):
            continue

        target = image_dir / f"{i}.png"
        log(f"Image {i} is missing, trying to fill it...")
        if i > 1 and image_exists(i - 1, image_dir):
            copyfile(image_dir / f"{i - 1}.png", target)
            log(f"Image {i} replaced with previous image {i - 1}")
            continue

        # The first missing image has no predecessor; use the next available one.
        next_index = next((j for j in range(i + 1, total_count + 1) if image_exists(j, image_dir)), None)
        if next_index is not None:
            copyfile(image_dir / f"{next_index}.png", target)
            log(f"Image {i} replaced with next image {next_index}")
        else:
            Image.new("RGB", (1024, 1024), (30, 30, 30)).save(target, "PNG")
            log(f"Image {i}: no illustration available; using a neutral background.")


def verify_all_images(total_count: int):
    missing = []
    for i in range(1, total_count + 1):
        if not image_exists(i):
            missing.append(i)

    if missing:
        log(f"[VERIFY] Images manquantes : {missing}")
    else:
        log("[VERIFY] Toutes les images sont présentes.")


# =========================================================
# MAIN
# =========================================================

async def main_image_gen(prompts):
    ensure_dirs()

    sem = asyncio.Semaphore(CONCURRENCY)

    tasks = [
        asyncio.create_task(generate_image(i + 1, prompt, sem))
        for i, prompt in enumerate(prompts)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Si jamais une vraie exception "hors boucle" a réussi à remonter
    for idx, result in enumerate(results, start=1):
        if isinstance(result, Exception):
            log(f"[MAIN] La tâche image {idx} a levé une exception non gérée : {repr(result)}")

    fill_missing_images(IMAGE_DIR, prompts)
    verify_all_images(len(prompts))
    log("Terminé.")


if __name__ == "__main__":
    asyncio.run(main_image_gen(sentences))
