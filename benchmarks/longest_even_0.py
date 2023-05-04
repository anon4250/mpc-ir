from UTIL import shared


def longest_even_0(Seq: shared[list[int]], N: int, Sym: shared[int]) -> shared[int]:
    """
    Computes the length of the longest regex of form (a*) which has an even length
    Sym is the symbol a
    """

    current_length = 0
    max_length = 0

    for i in range(1, N):
        if Seq[i] == Sym:
            current_length = current_length + 1
        else:
            current_length = 0

        tmp_max_len = max_length
        if current_length > max_length:
            tmp_max_len = current_length

        if tmp_max_len & 1 == 0:
            max_length = tmp_max_len

    return max_length


print(longest_even_0([0, 0, 1, 0, 0, 0], 6, 0))
