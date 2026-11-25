#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Helpers for running asynchronous code from synchronous callers."""

import asyncio


async def gather_with_keys(items, limit=None, timeout=None):
    """Run awaitables concurrently, keeping each result with its key.

    Exceptions are returned alongside their key instead of being raised,
    so that one failing awaitable does not discard the results of the
    others. It is up to the caller to tell results and exceptions apart.

    :param items: an iterable of (key, awaitable) pairs. A key can be any
        object identifying the awaitable to the caller.
    :param limit: the greatest number of awaitables allowed to run at
        once. Every awaitable is still scheduled up front, but one only
        starts running once fewer than ``limit`` others are in flight,
        which bounds the resources they hold open between them. None, or
        any value below 1, places no bound.
    :param timeout: seconds to allow for all of the awaitables together.
        Whatever has finished by then is returned; the rest are
        cancelled and come back as ``CancelledError`` against their own
        key, so one awaitable that never finishes costs its own result
        rather than everybody's. None waits indefinitely.
    :returns: a list of (key, result) tuples in the order of ``items``,
        where a result is either the value the awaitable produced or the
        exception it raised. Note that a cancelled awaitable yields
        ``CancelledError``, which is a BaseException and not an
        Exception, so callers testing for failures must take that into
        account.
    """
    items = list(items)

    if limit is not None and limit >= 1:
        semaphore = asyncio.Semaphore(limit)

        async def _run(awaitable):
            async with semaphore:
                return await awaitable

        scheduled = [_run(awaitable) for _, awaitable in items]
    else:
        scheduled = [awaitable for _, awaitable in items]

    # ensure_future rather than create_task: a caller is promised that
    # any awaitable will do, and create_task takes coroutines alone. An
    # interface returning a future or an object with __await__ would
    # otherwise raise TypeError here, before any of the work is run.
    tasks = [asyncio.ensure_future(item) for item in scheduled]

    if timeout is not None:
        await asyncio.wait(tasks, timeout=timeout)
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            # Let the cancellations land before collecting, so that
            # whatever each one held -- a BMC session, a socket -- is
            # released by its own cleanup rather than left to the
            # garbage collector.
            await asyncio.gather(*pending, return_exceptions=True)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [(key, result) for (key, _), result in zip(items, results)]
