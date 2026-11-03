import time
import os
import random
import selenium.webdriver
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import pickle
import re
from urllib.parse import urlparse

import chromedriver_autoinstaller


def set_cookies(driver, cookies_path):
    try:
        cookies = pickle.load(open(cookies_path, "rb"))
        driver.delete_all_cookies()

        for cookie in cookies:
            driver.add_cookie(cookie)

        driver.refresh()

    except FileNotFoundError:
        print("Cookies file not found. Waiting 2 minutes before creating the cookies file...")
        time.sleep(120)

        cookies = driver.get_cookies()
        pickle.dump(cookies, open(cookies_path, "wb"))

    time.sleep(5)


def start_driver(url, cookies_path, website_name, chromedriver_path):
    options = selenium.webdriver.ChromeOptions()
    options.add_argument("--disable-search-engine-choice-screen")

    driver = uc.Chrome(
        options=options,
        driver_executable_path=chromedriver_path,
        headless=False,
        use_subprocess=False,
    )

    driver.set_page_load_timeout(300)
    driver.maximize_window()

    driver.get(url)
    set_cookies(driver, cookies_path)

    print(f"Driver restarted and page loaded successfully for {website_name}")

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


def format_full_story_title(title: str) -> str:
    cleaned_title = re.sub(
        r"\[full\s*(story|parts)\]",
        "",
        title,
        flags=re.IGNORECASE
    ).strip()

    return f"[FULL STORY] {cleaned_title}"


def paste_content(driver, element, content):
    driver.execute_script(
        """
        const text = arguments[1];
        const dataTransfer = new DataTransfer();

        dataTransfer.setData("text/plain", text);

        const event = new ClipboardEvent("paste", {
            clipboardData: dataTransfer,
            bubbles: true
        });

        arguments[0].dispatchEvent(event);
        """,
        element,
        content
    )


def upload_video_from_spotify(driver, video_path, episode, base_path, account_index):
    try:
        print("Uploading...")
        print(f"Title: {episode['title']} from {episode['channel']}")

        time.sleep(5)

        try:
            driver.minimize_window()
            driver.maximize_window()
        except:
            pass

        time.sleep(5)

        upload_link = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center"

        try:
            driver.get(upload_link)

        except:
            print("Refresh failed. Restarting browser.")

            driver.quit()
            time.sleep(30)

            chromedriver_path = chromedriver_autoinstaller.install(True)

            driver = start_driver(
                upload_link,
                f"Cookies/cookies_tiktok_{account_index}.pkl",
                "TikTok",
                chromedriver_path
            )

        time.sleep(10)

        try:
            iframe = driver.find_element(
                By.CSS_SELECTOR,
                "iframe[src*='tiktokstudio/upload']"
            )

            driver.switch_to.frame(iframe)

        except:
            driver.switch_to.default_content()
            print("Upload page is not inside an iframe.")

        time.sleep(5)

        try:
            file_input = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='file']")
                )
            )

            absolute_path = os.path.abspath(
                os.path.join(str(base_path), str(video_path))
            )

            print(absolute_path)

            file_input.send_keys(absolute_path)

            print("Video file sent.")

        except Exception as e:
            print(f"Error uploading video: {e}")
            return False

        time.sleep(random.randint(600, 1200))

        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()

            text_input = WebDriverWait(driver, 60).until(
                EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, "div[contenteditable='true']")
                )
            )

            text_input.click()

            time.sleep(2)

            text_input.send_keys(Keys.CONTROL, "a")
            text_input.send_keys(Keys.DELETE)

            time.sleep(2)

            title = format_full_story_title(episode["title"])

            channel_name = episode.get("channel_name")
            if not channel_name:
                channel_url = episode["channel"]
                channel_path = urlparse(channel_url).path.strip("/")
                channel_name = channel_path.split("/")[-1].lstrip("@")

            paste_content(
                driver,
                text_input,
                f"{title}\n\n"
                f"Go check out {channel_name} on Spotify for more stories.\n"
                f"For any inquiries or claims, please DM me.\n\n"
                f"#fullstory #redditstories #redditreadings #storytime #longstory"
            )

            text_input.send_keys(Keys.ESCAPE)

            print("Caption updated.")

        except Exception as e:
            print(f"Error finding or updating title: {e}")
            return False

        time.sleep(20)

        try:
            publish_button = WebDriverWait(driver, 60).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//*[normalize-space(text())='Publier']"
                    )
                )
            )

            publish_button.click()

        except Exception as e:
            print(f"Error clicking publish button: {e}")
            return False

        time.sleep(200)

        try:
            publish_button = driver.find_element(
                By.XPATH,
                "//*[normalize-space(text())='Publier maintenant']"
            )

            publish_button.click()

        except:
            pass

        driver.switch_to.default_content()

        print("Uploaded.")

        time.sleep(30)

        os.remove(absolute_path)

        return True

    except Exception as e:
        print(f"Unexpected error: {e}")
        return False



