import asyncio, sys
from contextlib import suppress
import httpx
from creart import add_creator
from src.logger import LoggerCreator; add_creator(LoggerCreator)
from src.config import ConfigCreator; add_creator(ConfigCreator)
from src.api import APICreator; add_creator(APICreator)
from src.grpc.manager import WMCreator; add_creator(WMCreator)
from src.measurer import MeasurerCreator; add_creator(MeasurerCreator)

from creart import it
from src.rip import Ripper
from src.config import Config
from src.grpc.manager import WrapperManager
from src.flags import Flags
from src.url import AppleMusicURL, URLType
from src.api import WebAPI
from src.measurer import Measurer
from src.utils import check_dep

USAGE = """Usage: music-alac <url> [codec]
Download songs from Apple Music (or resolve Spotify URLs) at the best available quality.

Positional:
  url     Apple Music or Spotify URL (song/album/playlist)
  codec   alac (default), aac, aac-legacy, ec3, ac3

Options:
  --help  Show this help message
"""

def is_supported_url(raw_url: str) -> bool:
    return (
        "music.apple.com" in raw_url
        or "open.spotify.com" in raw_url
        or raw_url.startswith("spotify:")
    )

async def resolve_url(raw_url: str) -> str:
    if "open.spotify.com" not in raw_url and not raw_url.startswith("spotify:"):
        return raw_url

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=30.0)) as client:
        resp = await client.get("https://api.song.link/v1-alpha.1/links", params={"url": raw_url})
        resp.raise_for_status()
        data = resp.json()
        apple_url = data.get("linksByPlatform", {}).get("appleMusic", {}).get("url")
        if apple_url:
            print(f"Resolved Spotify URL to Apple Music: {apple_url}")
            return apple_url

        spotify_entity = data.get("entitiesByUniqueId", {}).get(data.get("entityUniqueId"), {})
        title = (spotify_entity.get("title") or "").strip()
        artist = (spotify_entity.get("artistName") or "").strip()
        entity = "song"
        if "/album/" in raw_url or raw_url.startswith("spotify:album:"):
            entity = "album"
        if not title or not artist:
            raise RuntimeError(f"Could not read Spotify metadata: {raw_url}")

        search_resp = await client.get("https://itunes.apple.com/search", params={
            "term": f"{artist} {title}",
            "media": "music",
            "entity": entity,
            "country": "US",
            "limit": 1,
        })
        search_resp.raise_for_status()
        results = search_resp.json().get("results", [])
        if not results:
            raise RuntimeError(f"Could not resolve Spotify URL to Apple Music: {raw_url}")

        apple_url = results[0].get("trackViewUrl") or results[0].get("collectionViewUrl")
        if not apple_url:
            raise RuntimeError(f"Could not resolve Spotify URL to Apple Music: {raw_url}")
        print(f"Resolved Spotify URL to Apple Music: {apple_url}")
        return apple_url

async def run_download(url, codec, ripper) -> bool:
    wm = it(WrapperManager)
    wm.set_fail_pending_handler(ripper.fail_pending_decrypts)
    decrypt_task = asyncio.create_task(wm.decrypt_init(
        on_success=ripper.on_decrypt_success,
        on_failure=ripper.on_decrypt_failed
    ))
    try:
        print(f"Downloading with codec: {codec}")
        match url.type:
            case URLType.Song:
                await ripper.rip_song(url, codec, Flags(force_save=True, language="en-US"))
                await asyncio.sleep(1)
            case URLType.Album:
                await ripper.rip_album(url, codec, Flags(force_save=True, language="en-US"))
                await ripper.download_manager.wait_until_idle()
            case URLType.Playlist:
                await ripper.rip_playlist(url, codec, Flags(force_save=True, language="en-US"))
                await ripper.download_manager.wait_until_idle()
            case _:
                print(f"Unsupported Apple Music URL type: {url.type}")
                return False
        dm = ripper.download_manager
        print(f"Done! {dm.ok} ok, {dm.fail} failed out of {dm.total}")
        return dm.fail == 0
    finally:
        decrypt_task.cancel()
        with suppress(asyncio.CancelledError):
            await decrypt_task

async def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(USAGE)
        return

    url_str = sys.argv[1]
    codec = sys.argv[2] if len(sys.argv) > 2 else it(Config).download.codecPriority[0]
    if codec not in ("alac", "aac", "aac-legacy", "ec3", "ac3"):
        print(f"Invalid codec: {codec}")
        print(USAGE)
        return

    if not is_supported_url(url_str):
        print("Only Apple Music and Spotify URLs are supported.")
        print(USAGE)
        return

    url_str = await resolve_url(url_str)
    url = AppleMusicURL.parse_url(url_str)
    if not url:
        print(f"Invalid Apple Music URL after resolution: {url_str}")
        return

    dep_ok, missing = check_dep()
    if not dep_ok:
        print(f"Missing dep: {missing}"); return

    config = it(Config); wm = it(WrapperManager)

    await asyncio.to_thread(it(WebAPI).init)
    await wm.init(config.instance.url, config.instance.secure)
    wm.status.cache_invalidate()
    st = await wm.status()
    if st.regions:
        print(f"Regions: {', '.join(st.regions)}")
    else:
        print("No regions available on wrapper-manager")

    while True:
        ripper = Ripper()
        ok = await run_download(url, codec, ripper)
        if ok:
            break
        try:
            answer = await asyncio.to_thread(input, "Retry failed items? [y/N]: ")
        except (EOFError, OSError):
            break
        if answer.strip().lower() not in ("y", "yes"):
            break

def real_main():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")

if __name__ == "__main__":
    real_main()
