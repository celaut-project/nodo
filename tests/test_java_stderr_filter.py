"""Tests for filtering the optional SLF4J startup notice from JVM stderr."""

import io
import unittest

from src.utils.java_dependency import _Slf4jStderrFilter


class Slf4jStderrFilterTests(unittest.TestCase):
    def test_hides_only_the_known_slf4j_notice(self):
        target = io.StringIO()
        stream = _Slf4jStderrFilter(target)

        stream.write('SLF4J: Failed to load class "org.slf4j.impl.StaticLoggerBinder".\n')
        stream.write("SLF4J: Defaulting to no-operation (NOP) logger implementation\n")
        stream.write("SLF4J: See http://www.slf4j.org/codes.html#StaticLoggerBinder for further details.\n")
        stream.write("A real Java error\n")
        stream.flush()

        self.assertEqual(target.getvalue(), "A real Java error\n")

    def test_handles_partial_writes_without_hiding_other_output(self):
        target = io.StringIO()
        stream = _Slf4jStderrFilter(target)

        stream.write("SLF4J: Defaulting to no-")
        stream.write("operation (NOP) logger implementation\nUseful warning")
        stream.flush()

        self.assertEqual(target.getvalue(), "Useful warning")


if __name__ == "__main__":
    unittest.main()
