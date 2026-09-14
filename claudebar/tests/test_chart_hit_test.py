"""The chart hover hit test, which maps a cursor x to a bar index."""

from ui_window import bar_index_at, _dim, _monochrome_glyph
from PIL import Image


class TestBarIndexAt:
    WIDTH = 348

    def test_first_and_last_bars(self):
        assert bar_index_at(0, 7, self.WIDTH) == 0
        assert bar_index_at(self.WIDTH, 7, self.WIDTH) == 6

    def test_each_bar_centre_maps_to_itself(self):
        n, gap = 7, 4
        slot = (self.WIDTH + gap) / n
        for i in range(n):
            centre = int(i * slot + slot / 2)
            assert bar_index_at(centre, n, self.WIDTH) == i

    def test_gap_belongs_to_the_bar_on_its_left(self):
        n, gap = 7, 4
        slot = (self.WIDTH + gap) / n
        edge = int(slot) - 1  # last pixel of the first slot
        assert bar_index_at(edge, n, self.WIDTH) == 0

    def test_outside_the_canvas_is_none(self):
        assert bar_index_at(-5, 7, self.WIDTH) is None
        assert bar_index_at(self.WIDTH + 10, 7, self.WIDTH) is None

    def test_no_bars_is_none(self):
        assert bar_index_at(10, 0, self.WIDTH) is None

    def test_single_bar_always_matches(self):
        assert bar_index_at(0, 1, self.WIDTH) == 0
        assert bar_index_at(self.WIDTH, 1, self.WIDTH) == 0


class TestDim:
    def test_dims_towards_black(self):
        assert _dim("#F59E0B", 0.5) == "#7a4f05"

    def test_an_unparseable_colour_passes_through(self):
        assert _dim("not-a-colour") == "not-a-colour"


class TestMonochromeGlyph:
    def test_an_opaque_mark_becomes_transparent_around_the_glyph(self):
        """A white card with a dark square must not render as a white block."""
        img = Image.new("RGB", (32, 32), (255, 255, 255))
        for x in range(8, 24):
            for y in range(8, 24):
                img.putpixel((x, y), (0, 0, 0))
        mask = _monochrome_glyph(img, canvas=32, box=20)
        assert mask.getpixel((16, 16)) > 200   # glyph is opaque
        assert mask.getpixel((1, 1)) < 20      # card is transparent

    def test_an_already_transparent_mark_keeps_its_shape(self):
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        for x in range(8, 24):
            for y in range(8, 24):
                img.putpixel((x, y), (77, 107, 254, 255))
        mask = _monochrome_glyph(img, canvas=32, box=20)
        assert mask.getpixel((1, 1)) == 0
        assert mask.getpixel((16, 16)) > 200

    def test_the_glyph_is_scaled_to_a_common_optical_size(self):
        small = Image.new("RGB", (64, 64), (255, 255, 255))
        for x in range(28, 36):
            for y in range(28, 36):
                small.putpixel((x, y), (0, 0, 0))
        big = Image.new("RGB", (64, 64), (255, 255, 255))
        for x in range(8, 56):
            for y in range(8, 56):
                big.putpixel((x, y), (0, 0, 0))

        def extent(mask):
            box = mask.getbbox()
            return max(box[2] - box[0], box[3] - box[1])

        assert abs(extent(_monochrome_glyph(small)) - extent(_monochrome_glyph(big))) <= 2
