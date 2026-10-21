class DefaultLanguageMiddleware:
    """Ignore the browser's Accept-Language so the site opens in LANGUAGE_CODE (Arabic).

    The language switch in the header still works through the language cookie.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.META.pop("HTTP_ACCEPT_LANGUAGE", None)
        return self.get_response(request)
