import argparse


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def quality_value(value):
    quality = str(value).strip().lower()
    if quality in {"best", "worst"}:
        return quality
    number = quality.removesuffix("p")
    if not number.isdigit() or int(number) < 1:
        raise argparse.ArgumentTypeError("清晰度必须是 best、worst 或正整数，例如 1080p")
    return f"{int(number)}p"


def nonnegative_number(value):
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须大于等于 0")
    return number
