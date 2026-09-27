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

import asyncio

from ironic.common import async_utils
from ironic.tests import base


class GatherWithKeysTestCase(base.TestCase):

    def test_empty(self):
        self.assertEqual([], asyncio.run(async_utils.gather_with_keys([])))

    def test_keeps_results_with_their_keys(self):
        async def value(val):
            return val

        items = [('first', value(1)), ('second', value(2))]
        self.assertEqual([('first', 1), ('second', 2)],
                         asyncio.run(async_utils.gather_with_keys(items)))

    def test_returns_exceptions(self):
        async def value(val):
            return val

        async def boom():
            raise RuntimeError('boom')

        items = [('ok', value(1)), ('bad', boom()), ('also ok', value(2))]
        results = asyncio.run(async_utils.gather_with_keys(items))

        self.assertEqual(['ok', 'bad', 'also ok'],
                         [key for key, _ in results])
        self.assertEqual(1, results[0][1])
        self.assertIsInstance(results[1][1], RuntimeError)
        self.assertEqual(2, results[2][1])

    def test_runs_concurrently(self):
        started = []

        async def track(name):
            started.append(name)
            # yield control, so a sequential implementation would finish
            # this coroutine before the next one is even started
            await asyncio.sleep(0)
            return len(started)

        items = [('first', track('first')), ('second', track('second'))]
        results = asyncio.run(async_utils.gather_with_keys(items))

        # both coroutines were started before either returned
        self.assertEqual([('first', 2), ('second', 2)], results)

    def _peak_concurrency(self, count, limit):
        """Run `count` awaitables and report how many ever overlapped."""
        in_flight = 0
        peak = 0

        async def track(index):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            # yield, giving every other scheduled coroutine the chance to
            # start before this one finishes and frees its slot
            await asyncio.sleep(0)
            in_flight -= 1
            return index

        items = [(i, track(i)) for i in range(count)]
        results = asyncio.run(
            async_utils.gather_with_keys(items, limit=limit))

        # whatever the bound, every awaitable still ran and kept its key
        self.assertEqual([(i, i) for i in range(count)], results)
        return peak

    def test_limit_bounds_concurrency(self):
        """No more than `limit` awaitables run at once."""
        self.assertEqual(3, self._peak_concurrency(count=10, limit=3))

    def test_limit_of_one_runs_serially(self):
        self.assertEqual(1, self._peak_concurrency(count=5, limit=1))

    def test_limit_above_count_does_not_restrict(self):
        self.assertEqual(4, self._peak_concurrency(count=4, limit=100))

    def test_no_limit_runs_all_at_once(self):
        """The bound is opt-in: callers passing nothing are unaffected."""
        self.assertEqual(8, self._peak_concurrency(count=8, limit=None))
        self.assertEqual(8, self._peak_concurrency(count=8, limit=0))

    def test_limit_returns_exceptions(self):
        """A failure under the bound still does not sink the others.

        The bound must release its slot when an awaitable raises, or the
        run would stall once `limit` of them had failed.
        """
        async def value(val):
            return val

        async def boom():
            raise RuntimeError('boom')

        items = [('bad1', boom()), ('bad2', boom()),
                 ('ok', value(1)), ('bad3', boom())]
        results = asyncio.run(
            async_utils.gather_with_keys(items, limit=2))

        self.assertEqual(['bad1', 'bad2', 'ok', 'bad3'],
                         [key for key, _ in results])
        self.assertIsInstance(results[0][1], RuntimeError)
        self.assertIsInstance(results[1][1], RuntimeError)
        self.assertEqual(1, results[2][1])
        self.assertIsInstance(results[3][1], RuntimeError)


class GatherAwaitableKindsTestCase(base.TestCase):
    """Any awaitable has to work, not just a coroutine.

    ManagementInterface.get_sensors_data_async promises that an
    implementation "may be marked as async or return an awaitable in
    any other way". asyncio.create_task takes coroutines alone, so a
    driver handing back a future or an object with __await__ raised
    TypeError out of here -- and in the conductor that is caught
    around the whole batch, costing every node its sensor data for
    that cycle rather than just the one driver.
    """

    def test_coroutine(self):
        async def value():
            return 'coro'

        self.assertEqual(
            [('k', 'coro')],
            asyncio.run(async_utils.gather_with_keys([('k', value())])))

    def test_future(self):
        async def run():
            async def value():
                return 'future'

            future = asyncio.ensure_future(value())
            return await async_utils.gather_with_keys([('k', future)])

        self.assertEqual([('k', 'future')], asyncio.run(run()))

    def test_object_with_await(self):
        class Awaitable:
            def __await__(self):
                async def value():
                    return 'custom'
                return value().__await__()

        self.assertEqual(
            [('k', 'custom')],
            asyncio.run(async_utils.gather_with_keys([('k', Awaitable())])))

    def test_future_with_a_limit(self):
        """The semaphore path has to take them too."""
        async def run():
            async def value():
                return 'future'

            future = asyncio.ensure_future(value())
            return await async_utils.gather_with_keys([('k', future)],
                                                      limit=4)

        self.assertEqual([('k', 'future')], asyncio.run(run()))


class GatherTimeoutTestCase(base.TestCase):
    """A timeout must cost only the awaitables that overran it."""

    @staticmethod
    def _slow():
        async def never():
            await asyncio.sleep(30)
        return never()

    @staticmethod
    def _quick(val):
        async def value():
            return val
        return value()

    def test_results_that_arrived_in_time_are_kept(self):
        items = [('quick', self._quick({'ok': 1})), ('slow', self._slow())]

        results = dict(asyncio.run(
            async_utils.gather_with_keys(items, timeout=0.05)))

        self.assertEqual({'ok': 1}, results['quick'])

    def test_the_overrunning_one_comes_back_cancelled(self):
        items = [('quick', self._quick(1)), ('slow', self._slow())]

        results = dict(asyncio.run(
            async_utils.gather_with_keys(items, timeout=0.05)))

        self.assertIsInstance(results['slow'], asyncio.CancelledError)

    def test_cancelled_results_are_not_exceptions(self):
        """CancelledError is a BaseException, which callers must expect.

        A caller testing ``isinstance(result, Exception)`` to spot
        failures would take this for sensor data.
        """
        items = [('slow', self._slow())]

        results = dict(asyncio.run(
            async_utils.gather_with_keys(items, timeout=0.05)))

        self.assertNotIsInstance(results['slow'], Exception)
        self.assertIsInstance(results['slow'], BaseException)

    def test_the_overrunning_one_is_really_cancelled(self):
        """Cancellation has to land, so held resources are released."""
        state = {}

        async def never():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state['cleaned_up'] = True
                raise

        asyncio.run(async_utils.gather_with_keys([('slow', never())],
                                                 timeout=0.05))

        self.assertTrue(state.get('cleaned_up'))

    def test_no_timeout_waits(self):
        items = [('a', self._quick(1)), ('b', self._quick(2))]

        self.assertEqual([('a', 1), ('b', 2)],
                         asyncio.run(async_utils.gather_with_keys(items)))

    def test_timeout_applies_with_a_limit(self):
        items = [('quick', self._quick(1)), ('slow', self._slow())]

        results = dict(asyncio.run(
            async_utils.gather_with_keys(items, limit=4, timeout=0.05)))

        self.assertEqual(1, results['quick'])
        self.assertIsInstance(results['slow'], asyncio.CancelledError)
