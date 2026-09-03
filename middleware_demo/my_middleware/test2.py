class Box[T]:
    """A simple generic container class."""

    def __init__(self, value: T) -> None:
        self.value = value

    def get_value(self) -> T:
        return self.value


b = Box(42)
