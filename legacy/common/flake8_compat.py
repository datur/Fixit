# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import ast
import io
import tokenize
from functools import lru_cache
from typing import Any, List, Optional, Type

from flake8.checker import (
    FileChecker as Flake8FileChecker,
    Manager as Flake8CheckerManager,
)
from flake8.formatting.base import BaseFormatter as Flake8BaseFormatter
from flake8.main.application import Application as Flake8BaseApplication
from flake8.processor import FileProcessor as Flake8FileProcessor
from flake8.style_guide import Violation as Flake8Violation

from fixit.common.pseudo_rule import PseudoContext
from fixit.common.report import BaseLintRuleReport


__all__ = ["Flake8LintRuleReport"]


class Flake8CompatAccumulatingFormatter(Flake8BaseFormatter):
    """
    Handles errors by writing them to the provided `accumulator` object. Does not write
    anything to stdout/stderr/a file.
    """

    def __init__(
        self, options: argparse.Namespace, accumulator: List[Flake8Violation]
    ) -> None:
        super().__init__(options)
        self.accumulator: List[Flake8Violation] = accumulator

    def handle(self, error: Flake8Violation) -> None:
        self.accumulator.append(error)

    def start(self) -> None:
        # By default, this may open a file. Don't do that!
        pass

    def stop(self) -> None:
        # By default, this may close a file. Don't do that!
        pass

    def write(self, line: Optional[str], source: Optional[str]) -> None:
        # Responsible for writing output to stdout/stderr/etc. Don't do that!
        pass


class Flake8CompatFileProcessor(Flake8FileProcessor):
    def __init__(self, *args: Any, context: PseudoContext, **kwargs: Any) -> None:
        self.context: PseudoContext = (
            context  # super calls read_lines; do this before super()
        )
        super().__init__(*args, **kwargs)

    @property
    def file_tokens(self) -> List[tokenize.TokenInfo]:
        tokens_iter = iter(self.context.tokens)
        first_token = next(tokens_iter)  # strip the leading ENCODING token
        assert first_token.type == tokenize.ENCODING
        return list(tokens_iter)

    def build_ast(self) -> ast.Module:
        return self.context.ast_tree

    def read_lines(self) -> List[str]:
        encoding, __ = tokenize.detect_encoding(
            io.BytesIO(self.context.source).readline
        )
        decoded = self.context.source.decode(encoding)
        return io.StringIO(decoded).readlines()


class Flake8CompatFileChecker(Flake8FileChecker):
    def __init__(self, *args: Any, context: PseudoContext, **kwargs: Any) -> None:
        self.context = context  # super calls _make_processor; do this before super()
        super().__init__(*args, **kwargs)

    def _make_processor(self) -> Flake8CompatFileProcessor:
        return Flake8CompatFileProcessor(
            self.filename, self.options, context=self.context
        )


class Flake8CompatCheckerManager(Flake8CheckerManager):
    def __init__(self, *args: Any, context: PseudoContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.context = context

    def run(self) -> None:
        # Normally this will detect the number of jobs and use multiprocessing if it's
        # supported by the interpreter.
        #
        # Our lint framework does its own parallelization, so we want to disable that
        # here.
        self.run_serial()

    def make_checkers(self, paths: Optional[List[str]] = None) -> None:
        assert paths is not None
        assert len(paths) == 1

        checks = self.checks.to_dictionary()
        checkers = [
            Flake8CompatFileChecker(
                paths[0], checks, self.options, context=self.context
            )
        ]
        # Necessary for the flake8 changes introduced here https://github.com/PyCQA/flake8/commit/bfb79b46c807168dbc25fd1e9e41359c4558256f
        # Flake8's Manager class now uses the parameter self._all_checkers to store the checkers
        self._all_checkers = self.checkers = [
            checker for checker in checkers if checker.should_process
        ]


class Flake8CompatApplication(Flake8BaseApplication):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.accumulator: List[Flake8Violation] = []

    def reset(self, context: PseudoContext) -> None:
        # An Application isn't really intended to be used multiple times, but we want to
        # avoid re-instantiating it because of the performance overhead, so let's just
        # reset the file checker manager. The file checker manager is the thing that
        # actually holds the reports generated by checkers.
        self.file_checker_manager = Flake8CompatCheckerManager(
            style_guide=self.guide,
            arguments=self.args,
            checker_plugins=self.check_plugins,
            context=context,
        )
        # And clear the list that Flake8CompatAccumulatingFormatter writes to
        self.accumulator.clear()

    def make_formatter(
        self, formatter_class: Optional[Type[Flake8BaseFormatter]] = None
    ) -> None:
        options = self.options
        assert options is not None
        # ignores formatter_class if given, we don't support flake8 formatters
        self.formatter = Flake8CompatAccumulatingFormatter(options, self.accumulator)

    def report_statistics(self) -> None:
        pass

    def report_benchmarks(self) -> None:
        pass


class Flake8LintRuleReport(BaseLintRuleReport):
    pass


@lru_cache(maxsize=1)
def get_cached_application_instance() -> Flake8CompatApplication:
    """
    The application object is somewhat expensive to construct (it reads flake8
    configuration files), so there shouldn't be more than one instance per worker
    process.
    """
    application = Flake8CompatApplication()
    # disable noqa because we have our own ignore system
    application.initialize(["--disable-noqa"])
    return application
