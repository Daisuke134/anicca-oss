"""Browser-based login flow for note.com.

Uses Playwright to open a browser for manual user login,
then extracts session cookies for API authentication.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from playwright.async_api import TimeoutError as PlaywrightTimeout

from note_mcp.auth.session import SessionManager
from note_mcp.browser.manager import BrowserManager
from note_mcp.models import LoginError, Session

if TYPE_CHECKING:
    from playwright.async_api import Page


# note.com URLs
NOTE_LOGIN_URL = "https://note.com/login"
NOTE_HOME_URL = "https://note.com/"
NOTE_ACCOUNT_SETTINGS_URL = "https://note.com/settings/account"

# Required cookie names (note_gql_auth_token is optional, set on-demand by note.com)
REQUIRED_COOKIES = ["_note_session_v5"]
OPTIONAL_COOKIES = ["note_gql_auth_token", "XSRF-TOKEN"]

# Default timeout for login (5 minutes)
DEFAULT_LOGIN_TIMEOUT = 300

# Login form selectors
# note.com's login form now identifies fields by id, not name (name="" on both inputs,
# verified 2026-07-12 by dumping every <input> on https://note.com/login: id="email" /
# id="password", no name attribute at all) -- the old name= selectors matched nothing and
# _perform_auto_login always raised "ログインフォームが見つかりません" after a 10s timeout.
LOGIN_EMAIL_SELECTOR = '#email'
LOGIN_PASSWORD_SELECTOR = '#password'
# The login button is type="button" (not "submit") with no text/name attribute Playwright
# can key off directly except its visible label (verified 2026-07-12: outerHTML shows
# type="button" data-type="primary", no type="submit" anywhere on the page) -- match by
# the visible Japanese label instead of the no-longer-present submit type.
LOGIN_SUBMIT_SELECTOR = 'button:has-text("ログイン")'

# Obstacle detection selectors
# note.com embeds Google reCAPTCHA v3 unconditionally on every /login render:
# a `.grecaptcha-badge` + `.grecaptcha-logo` (Google's required branding icon,
# genuinely visible in the bottom-right corner of every page load) plus a
# hidden `g-recaptcha-response` textarea and bare `<iframe>`. A selector that
# matches `[class*="recaptcha"]` broadly -- even gated on Playwright
# `visible=true` -- still matches that branding badge on 100% of loads
# (verified 2026-07-12: `.grecaptcha-badge` reports `is_visible() == True` on
# a pristine, pre-interaction login page), so it can never distinguish a real
# challenge from normal page furniture. The actual interactive widget slot
# note.com would populate if it challenges a login is
# `.g-recaptcha.o-login__recaptcha` -- present but `is_visible() == False`
# unless a challenge is actually shown. Target that specific container only.
RECAPTCHA_SELECTOR = '.g-recaptcha.o-login__recaptcha'
TWO_FACTOR_SELECTOR = '[data-testid="two-factor"], input[name="otp"], input[placeholder*="認証コード"]'

# Timeout constants (in milliseconds)
EMAIL_INPUT_TIMEOUT_MS = 10000
NETWORK_IDLE_TIMEOUT_MS = 15000
REDIRECT_TIMEOUT_MS = 15000


def extract_session_cookies(cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Extract required session cookies from browser cookies.

    Args:
        cookies: List of cookie dictionaries from Playwright

    Returns:
        Dictionary with required and optional cookie name-value pairs

    Raises:
        ValueError: If required cookies are missing
    """
    result: dict[str, str] = {}

    all_cookies = REQUIRED_COOKIES + OPTIONAL_COOKIES
    for cookie in cookies:
        name = cookie.get("name", "")
        if name in all_cookies:
            result[name] = cookie.get("value", "")

    # Validate required cookies only
    for required_cookie in REQUIRED_COOKIES:
        if required_cookie not in result:
            raise ValueError(f"Missing required cookie: {required_cookie}")

    return result


async def get_user_from_browser(page: Any) -> dict[str, Any]:
    """Get user info from browser page using JavaScript.

    Extracts user information from note.com's account settings page.
    Uses the profile link element to extract the username.

    Args:
        page: Playwright page object (should be on /settings/account page)

    Returns:
        User info dictionary with 'id' and 'urlname' fields

    Raises:
        ValueError: If user info cannot be retrieved
    """
    import logging

    logger = logging.getLogger(__name__)

    # Try to get user info from note.com's account settings page
    user_info = await page.evaluate("""
        () => {
            // Method 1: Best method - find <a href="/settings/account/note_id"> and get <p> inside
            // Structure: <a href="/settings/account/note_id"><div><h3>note ID</h3><p>USERNAME</p></div></a>
            const noteIdLink = document.querySelector('a[href="/settings/account/note_id"]');
            if (noteIdLink) {
                const pElement = noteIdLink.querySelector('p');
                if (pElement) {
                    const username = (pElement.textContent || '').trim();
                    if (username && /^[a-zA-Z0-9_-]+$/.test(username)) {
                        return { id: '', urlname: username };
                    }
                }
            }

            // Method 2: Try __NEXT_DATA__ (Next.js server-side props)
            if (window.__NEXT_DATA__ && window.__NEXT_DATA__.props) {
                const pageProps = window.__NEXT_DATA__.props.pageProps;
                if (pageProps && pageProps.currentUser) {
                    return {
                        id: String(pageProps.currentUser.id || ''),
                        urlname: pageProps.currentUser.urlname || ''
                    };
                }
            }

            // Method 3: Search localStorage for user info
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key) {
                    try {
                        const value = localStorage.getItem(key);
                        const data = JSON.parse(value);
                        const search = (obj, depth = 0) => {
                            if (depth > 5 || !obj || typeof obj !== 'object') return null;
                            if (obj.urlname && typeof obj.urlname === 'string') {
                                return obj.urlname;
                            }
                            for (const v of Object.values(obj)) {
                                const result = search(v, depth + 1);
                                if (result) return result;
                            }
                            return null;
                        };
                        const urlname = search(data);
                        if (urlname) {
                            return { id: '', urlname: urlname };
                        }
                    } catch (e) {
                        // Skip non-JSON values
                    }
                }
            }

            // Return empty if no username found
            return { id: '', urlname: '' };
        }
    """)

    logger.debug(f"Browser user info result: {user_info}")

    if user_info and (user_info.get("id") or user_info.get("urlname")):
        return {
            "id": str(user_info.get("id", "")),
            "urlname": user_info.get("urlname", ""),
        }

    raise ValueError("Could not retrieve user info from browser")


async def get_current_user(cookies: dict[str, str], xsrf_token: str | None = None) -> dict[str, Any]:
    """Get current user info from note.com API.

    Args:
        cookies: Session cookies for authentication
        xsrf_token: XSRF token for CSRF protection

    Returns:
        User info dictionary with 'id' and 'urlname' fields

    Raises:
        ValueError: If user info cannot be retrieved
    """
    import httpx

    async with httpx.AsyncClient() as client:
        # Build cookie header
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

        # Build headers with XSRF token if available
        headers: dict[str, str] = {
            "Cookie": cookie_header,
            "Accept": "application/json",
        }
        if xsrf_token:
            headers["X-XSRF-TOKEN"] = xsrf_token

        response = await client.get(
            "https://note.com/api/v1/stats/pv",
            headers=headers,
        )

        if response.status_code != 200:
            raise ValueError(f"Failed to get user info: HTTP {response.status_code}")

        data = response.json()

        # Extract user info from response
        # The stats/pv endpoint includes user info
        user_data = data.get("data", {})
        user_id = user_data.get("user_id") or user_data.get("id")
        urlname = user_data.get("urlname") or user_data.get("username")

        if not user_id or not urlname:
            # Try alternative endpoint
            response = await client.get(
                "https://note.com/api/v2/self",
                headers=headers,
            )

            if response.status_code != 200:
                raise ValueError(f"Failed to get user info: HTTP {response.status_code}")

            data = response.json()
            user_data = data.get("data", {})
            user_id = user_data.get("id", "")
            urlname = user_data.get("urlname", "")

        if not user_id:
            raise ValueError("Could not retrieve user ID")

        return {"id": str(user_id), "urlname": urlname or ""}


async def _check_login_obstacles(page: Page) -> None:
    """ログイン障害（reCAPTCHA/2FA）を検出する。

    Args:
        page: Playwrightページオブジェクト

    Raises:
        LoginError: 障害検出時
    """
    # reCAPTCHA検出
    # note.com renders Google reCAPTCHA v3 unconditionally on every load of
    # /login (verified 2026-07-12: a fresh, pre-interaction page already
    # contains .g-recaptcha, .grecaptcha-badge, .grecaptcha-logo, a
    # g-recaptcha-response textarea, and a bare <iframe> -- all matched by
    # RECAPTCHA_SELECTOR, all invisible/off-screen since v3 has no checkbox
    # UI unless note.com actually challenges the login). A plain
    # `.count() > 0` therefore fires on 100% of logins, including successful
    # ones, and made this check useless. Only a genuine v2-style challenge
    # renders `.g-recaptcha.o-login__recaptcha` (or its iframe) *visibly* --
    # gate on visibility, not presence.
    recaptcha = page.locator(f"{RECAPTCHA_SELECTOR} >> visible=true")
    if await recaptcha.count() > 0:
        raise LoginError(
            code="RECAPTCHA_DETECTED",
            message="reCAPTCHAが検出されました",
            resolution="手動でログインしセッションを保存してください",
        )

    # 2FA検出（同様に、実際に表示されている場合のみ障害とみなす）
    two_factor = page.locator(f"{TWO_FACTOR_SELECTOR} >> visible=true")
    if await two_factor.count() > 0:
        raise LoginError(
            code="TWO_FACTOR_REQUIRED",
            message="二段階認証が要求されています",
            resolution="手動でログインしセッションを保存してください",
        )

    # ログインエラー検出（認証情報エラー）
    error_message = page.locator('[class*="error"], [class*="alert"]')
    if await error_message.count() > 0:
        error_text = await error_message.first.text_content()
        if error_text and ("パスワード" in error_text or "メールアドレス" in error_text):
            raise LoginError(
                code="INVALID_CREDENTIALS",
                message="認証情報が無効です",
                resolution="ユーザー名とパスワードを確認してください",
            )


async def _perform_auto_login(page: Page, username: str, password: str) -> None:
    """自動ログインを実行する。

    reCAPTCHAや2FAが検出された場合はLoginErrorを送出する。

    Args:
        page: Playwrightページオブジェクト
        username: ログインユーザー名（メールアドレス）
        password: ログインパスワード

    Raises:
        LoginError: reCAPTCHA/2FA検出時、認証失敗時
    """
    # ユーザー名入力
    email_input = page.locator(LOGIN_EMAIL_SELECTOR)
    try:
        await email_input.wait_for(state="visible", timeout=EMAIL_INPUT_TIMEOUT_MS)
    except PlaywrightTimeout as e:
        raise LoginError(
            code="FORM_NOT_FOUND",
            message="ログインフォームが見つかりません",
            resolution="note.comのログインページが正しく読み込まれていることを確認してください",
        ) from e

    # note.com's login form is a Vue component with a v-model bound to the visible
    # input box and a computed `disabled` on the submit button that only flips to
    # false once Vue's own input handler sees the value change. `.fill()` sets the
    # underlying DOM `.value` and fires a single synthetic "input" event, but Vue
    # never observes it (verified 2026-07-12: after fill(), `#email`/`#password`
    # read back the filled value via JS, yet the rendered boxes stay visibly empty
    # and the button keeps `disabled="disabled"` -- Locator.click() then times out
    # after 30s with "element is not enabled"). Fix: type like a human via
    # press_sequentially(), which dispatches real keydown/keypress/input/keyup per
    # character, the same events Vue's listener is wired for.
    #
    # A second, independent race exists even with real typing: if keystrokes start
    # immediately after "domcontentloaded", Vue's hydration hasn't finished
    # attaching its v-model listener yet, so the first 1-2 characters of the very
    # first field get silently swallowed (verified: "contact@..." became
    # "ontact@..." then "ntact@..." depending on timing -- login then fails with
    # "メールアドレスおよびパスワードをご確認ください" even though the button was
    # enabled and clickable). Fix: wait for networkidle + a short settle delay
    # before touching the form, and click-then-pause before each field's keystrokes
    # so hydration has landed and focus has committed.
    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    await page.wait_for_timeout(500)

    await email_input.click()
    await page.wait_for_timeout(200)
    await email_input.press_sequentially(username, delay=50)

    # パスワード入力
    password_input = page.locator(LOGIN_PASSWORD_SELECTOR)
    await page.wait_for_timeout(200)
    await password_input.click()
    await page.wait_for_timeout(200)
    await password_input.press_sequentially(password, delay=50)
    await page.wait_for_timeout(300)

    # ログインボタンクリック
    submit_button = page.locator(LOGIN_SUBMIT_SELECTOR)
    await submit_button.click()

    # ログイン結果を待機
    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)

    # 障害検出
    await _check_login_obstacles(page)


async def login_with_browser(
    timeout: int = DEFAULT_LOGIN_TIMEOUT,
    credentials: tuple[str, str] | None = None,
) -> Session:
    """Open browser for login and extract session.

    If credentials are provided, performs automatic login.
    If a saved session exists, injects cookies into browser to restore session.
    Otherwise, opens the note.com login page for manual login.

    Args:
        timeout: Maximum time to wait for login (seconds)
        credentials: Optional tuple of (username, password) for automatic login.
            If provided, attempts automatic login instead of waiting for manual input.

    Returns:
        Session object with cookies and user info

    Raises:
        TimeoutError: If login is not completed within timeout
        ValueError: If required cookies are not found
        LoginError: If automatic login encounters obstacles (reCAPTCHA, 2FA, invalid credentials)
    """
    import logging

    # Configure logging to file for debugging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("/tmp/note_mcp_login.log"),
            logging.StreamHandler(),
        ],
    )
    logger = logging.getLogger(__name__)

    manager = BrowserManager.get_instance()

    # Close existing browser to ensure fresh context
    logger.info("Closing existing browser...")
    await manager.close()

    # Headful mode is for a human watching a manual login; when credentials are supplied
    # this is a fully automatic login and nobody needs to see the window. On a headless
    # server invocation (no attached interactive Window Server session) a headful
    # chromium.launch() hangs forever at 0% CPU with no error (verified 2026-07-12: two
    # note-mcp processes sat idle for 5+ minutes, no browser process ever appeared) -- use
    # headless whenever we already have credentials to drive the login ourselves.
    use_headless = credentials is not None
    logger.info(f"Getting fresh page (headless={use_headless})...")
    page = await manager.get_page(headless=use_headless)

    # Check for saved session and inject cookies if available
    session_manager = SessionManager()
    saved_session = session_manager.load()

    if saved_session and not saved_session.is_expired() and saved_session.cookies:
        logger.info("Found saved session, injecting cookies into browser...")
        # Convert saved cookies to Playwright format
        playwright_cookies: list[dict[str, Any]] = []
        for name, value in saved_session.cookies.items():
            playwright_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".note.com",
                    "path": "/",
                }
            )
        await page.context.add_cookies(playwright_cookies)  # type: ignore[arg-type]
        logger.info(f"Injected {len(playwright_cookies)} cookies")

        # Navigate to home page to check if session is valid
        logger.info(f"Navigating to {NOTE_HOME_URL} to verify session...")
        await page.goto(NOTE_HOME_URL, wait_until="domcontentloaded")
    else:
        logger.info("No saved session or session expired, navigating to login page...")
        # Navigate to login page
        await page.goto(NOTE_LOGIN_URL, wait_until="domcontentloaded")

    current_url = page.url
    logger.info(f"Current URL after navigation: {current_url}")

    # Define what a logged-in URL looks like
    def is_logged_in(url: str) -> bool:
        # Must be on note.com but NOT on login-related pages
        if not url.startswith(NOTE_HOME_URL):
            return False
        # Exclude login and auth pages
        login_paths = ["/login", "/signup", "/auth", "/oauth"]
        result = all(f"note.com{path}" not in url for path in login_paths)
        logger.debug(f"is_logged_in({url}) = {result}")
        return result

    # Check if already logged in (URL changed during navigation)
    if is_logged_in(current_url):
        logger.info("Already logged in, extracting cookies...")
    elif credentials:
        # Automatic login with provided credentials
        username, password = credentials
        logger.info(f"Attempting automatic login for user: {username}")
        await _perform_auto_login(page, username, password)
        logger.info("Automatic login completed, verifying...")

        # Verify login succeeded by checking URL
        current_url = page.url
        if not is_logged_in(current_url):
            # Wait for redirect after login
            try:
                await page.wait_for_url(
                    is_logged_in,
                    timeout=REDIRECT_TIMEOUT_MS,
                )
                logger.info("Login redirect detected!")
            except PlaywrightTimeout as e:
                raise LoginError(
                    code="LOGIN_TIMEOUT",
                    message="ログイン後のリダイレクトがタイムアウトしました",
                    resolution="認証情報を確認するか、手動でログインしてください",
                ) from e
    else:
        # Wait for user to complete login (redirected away from login page)
        # This will block until user manually logs in via the browser
        logger.info("Waiting for user to complete login...")
        try:
            await page.wait_for_url(
                is_logged_in,
                timeout=timeout * 1000,  # Convert to milliseconds
            )
            logger.info("Login detected!")
        except Exception as e:
            raise TimeoutError(f"Login not completed within {timeout} seconds") from e

    # Navigate to account settings page to ensure all cookies are set
    # and to extract user info from the profile link
    logger.info(f"Navigating to account settings to extract user info: {NOTE_ACCOUNT_SETTINGS_URL}")
    await page.goto(NOTE_ACCOUNT_SETTINGS_URL, wait_until="domcontentloaded")
    # Wait a bit for cookies to be set
    import asyncio

    await asyncio.sleep(1)
    logger.info(f"Account settings URL: {page.url}")

    # Extract cookies from browser context
    logger.info("Extracting cookies from browser context...")
    final_cookies = await page.context.cookies()
    logger.info(f"Found {len(final_cookies)} cookies")

    # Log cookie names for debugging
    cookie_names = [c.get("name", "") for c in final_cookies]
    logger.info(f"Cookie names: {cookie_names}")

    # Convert Cookie objects to dicts for extraction
    final_cookie_dicts: list[dict[str, Any]] = [
        {"name": c.get("name", ""), "value": c.get("value", "")} for c in final_cookies
    ]
    cookies = extract_session_cookies(final_cookie_dicts)

    # Extract XSRF token for API calls
    xsrf_token: str | None = None
    for cookie in final_cookies:
        if cookie.get("name") == "XSRF-TOKEN":
            xsrf_token = cookie.get("value")
            break

    # Get user info - try multiple methods
    user_id = ""
    username = ""

    # Method 1: Try to get from browser page first (most reliable)
    try:
        user_info = await get_user_from_browser(page)
        user_id = user_info["id"]
        username = user_info["urlname"]
        logger.info(f"User info from browser: {username} (ID: {user_id})")
    except ValueError as e:
        logger.debug(f"Browser method failed: {e}")

    # Method 2: Click on profile avatar/link and extract username from URL
    if not username:
        try:
            logger.info("Trying to get username by clicking on profile link...")

            # Try to find and click the profile avatar/link in header
            # This usually appears as an image link that navigates to /{username}
            clicked = await page.evaluate("""
                () => {
                    // Look for profile avatar in header (usually an img wrapped in an anchor)
                    const headerAvatars = document.querySelectorAll('header a img, header button img');
                    for (const img of headerAvatars) {
                        const link = img.closest('a');
                        if (link) {
                            const href = link.getAttribute('href');
                            // Check if it looks like a profile link
                            if (href && href.match(/^\\/[a-zA-Z0-9_-]+$/)) {
                                link.click();
                                return href;
                            }
                        }
                    }

                    // Try clicking on any link in header that matches username pattern
                    const headerLinks = document.querySelectorAll('header a[href^="/"]');
                    const systemPaths = ['login', 'signup', 'search', 'notifications',
                        'settings', 'premium', 'contests', 'hashtag', 'sitesettings',
                        'explore', 'ranking', 'magazine', 'api', 'n', 'm', 'note'];
                    for (const link of headerLinks) {
                        const href = link.getAttribute('href');
                        if (href) {
                            const match = href.match(/^\\/([a-zA-Z0-9_-]+)$/);
                            if (match && !systemPaths.includes(match[1].toLowerCase())) {
                                link.click();
                                return href;
                            }
                        }
                    }

                    return null;
                }
            """)

            if clicked:
                logger.info(f"Clicked on profile link: {clicked}")
                # Wait for navigation
                await asyncio.sleep(1)

                # Extract username from current URL
                current_url = page.url
                logger.info(f"Current URL after navigation: {current_url}")

                # Parse URL to get username: https://note.com/{username}
                import re

                match = re.match(r"https://note\.com/([a-zA-Z0-9_-]+)", current_url)
                if match:
                    username = match.group(1)
                    user_id = username
                    logger.info(f"Extracted username from URL: {username}")
        except Exception as e:
            logger.warning(f"Profile navigation method failed: {e}")

    # Method 3: Fallback to API if browser method failed
    if not username:
        try:
            user_info = await get_current_user(cookies, xsrf_token=xsrf_token)
            user_id = user_info["id"]
            username = user_info["urlname"]
            logger.info(f"User info from API: {username} (ID: {user_id})")
        except ValueError as e:
            # Article 6: No placeholder values allowed
            # All three methods failed to retrieve user info
            raise ValueError(
                "Failed to retrieve user information after successful login. "
                "All methods (noteInitData, profile URL, API) failed. "
                f"Last error: {e}"
            ) from e

    # Create session
    session = Session(
        cookies=cookies,
        user_id=user_id,
        username=username,
        expires_at=None,  # No explicit expiry from cookies
        created_at=int(time.time()),
    )

    # Save session to keyring
    session_manager = SessionManager()
    session_manager.save(session)

    return session

