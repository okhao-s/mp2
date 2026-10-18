def under_challenge(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in ("cf-challenge", "just a moment...", "cdn-cgi/challenge-platform",
                                  "attention required! | cloudflare", "cf_chl_opt"))
