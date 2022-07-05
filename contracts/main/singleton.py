class Singleton:
    """
    A non-thread-safe helper class to ease implementing singletons.
    This should be used as a decorator -- not a metaclass -- to the
    class that should be a singleton.

    The decorated class must define at least two methods:

    * `__init__`: A simple constructor that takes no arguments.
    * `__str__`: A `str` representation of the object.

    """

    def __init__(self, decorated):
        self._decorated = decorated

    def __call__(self, *args, **kwargs):
        """
        Returns the singleton instance. Upon its first call, it creates a
        new instance of the decorated class and calls its `__init__` method.
        On all subsequent calls, the already created instance is returned.

        """

        if not hasattr(self, '_instance'):
            self._instance = self._decorated(*args, **kwargs)
        return self._instance

    def __str__(self):
        """
        Returns a `str` representation of the object.

        """

        return '<{name} instance>'.format(name=self._decorated.__name__)

    def __repr__(self):
        """
        Returns a `repr` representation of the object.

        """

        return '{name}()'.format(name=self._decorated.__name__)