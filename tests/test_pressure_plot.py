import unittest
from qt_support import APP
from ui.canvas.pressure_plot_widget import PressurePlotWidget


class PressurePlotTests(unittest.TestCase):
    def setUp(self):
        self.plot = PressurePlotWidget()
        self.addCleanup(self.plot.close)

    def test_reset_view_does_not_add_a_pressure_sample(self):
        self.plot.update_plot(0, 10, True, False)
        self.plot.update_plot(.1, 20, True, False)
        self.plot.reset_zoom(True, False)
        self.plot.reset_zoom(False, True)
        self.assertEqual(self.plot.times, [0, .1])
        self.assertEqual(self.plot.pressures, [10, 20])

    def test_zero_first_sample_has_nonnegative_nonempty_axis_in_both_modes(self):
        for auto_x in (True, False):
            self.plot.clear_plot()
            self.plot.update_plot(0, 10, auto_x, False)
            left, right = self.plot.ax.get_xlim()
            self.assertEqual(left, 0)
            self.assertGreater(right, left)
