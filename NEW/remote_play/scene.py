"""
The demo scene: a deliberately GPU-heavy real-time 3D 'pulse city'.

Hundreds of individually-lit, shadow-casting cubes animate every frame under a
sweeping shadow-casting directional light, with a player-controlled orb flying
above them. It's built to be smooth on a real GPU and to visibly struggle on
integrated graphics/CPU rendering -- that gap is the whole point of the demo.

This module only builds/updates the scene. It knows nothing about networking:
`app.py` decides whether input comes from the local keyboard or a LAN client,
and whether frames get streamed anywhere.
"""

import math
import random

from ursina import Entity, Text, Vec3, camera, color, time
from ursina.shaders import lit_with_shadows_shader
from ursina.lights import DirectionalLight

GRID_SPACING = 2.2
BASE_HALF_WIDTH = 12    # first wave: (2*12+1)^2 = 625 cubes
RING_GROWTH = 6         # each extra wave grows the grid outward by this many cells
MAX_WAVES = 3


class PulseCity:
    def __init__(self):
        Entity.default_shader = lit_with_shadows_shader

        self.ground = Entity(
            model='plane', scale=80, color=color.rgb(18, 18, 24),
            texture='white_cube', texture_scale=(80, 80),
        )

        self.sun = DirectionalLight(shadow_map_resolution=(2048, 2048))
        self.sun.update_bounds()

        self.cubes = []
        self._waves_spawned = 0
        self.spawn_wave()

        self.player = Entity(
            model='sphere', color=color.azure, scale=1.4,
            position=(0, 6, 0),
        )
        self._player_vy = 0.0

        self.info_text = Text(
            text='', position=(-0.85, 0.47), scale=1.6, color=color.white,
        )
        self.mode_text = Text(
            text='', position=(-0.85, 0.40), scale=1.1, color=color.yellow,
        )

        self._fps_samples = []
        self._t = 0.0

    def spawn_wave(self):
        if self._waves_spawned >= MAX_WAVES:
            return
        prev_half = BASE_HALF_WIDTH + (self._waves_spawned - 1) * RING_GROWTH
        new_half = BASE_HALF_WIDTH + self._waves_spawned * RING_GROWTH
        for ix in range(-new_half, new_half + 1):
            for iz in range(-new_half, new_half + 1):
                if self._waves_spawned > 0 and max(abs(ix), abs(iz)) <= prev_half:
                    continue  # already spawned by an earlier wave
                x = ix * GRID_SPACING
                z = iz * GRID_SPACING
                hue = (math.atan2(iz, ix) / math.tau) % 1.0
                cube = Entity(
                    model='cube',
                    position=(x, 0, z),
                    scale=(1.6, 1, 1.6),
                    color=color.hsv(hue * 360, 0.75, 0.9),
                )
                cube.phase = random.uniform(0, math.tau)
                self.cubes.append(cube)
        self._waves_spawned += 1

    def object_count(self):
        return len(self.cubes) + 2  # + player + ground

    def update(self, inp: dict, dt_override=None):
        dt = dt_override if dt_override is not None else time.dt
        self._t += dt

        # animate every cube -> forces shadow map + geometry updates every frame
        for cube in self.cubes:
            h = 1.2 + math.sin(self._t * 2.5 + cube.phase) * 1.1
            cube.scale_y = max(0.15, h)
            cube.y = h / 2

        # sweep the sun slowly so shadows are never static
        self.sun.rotation = (
            45 + math.sin(self._t * 0.15) * 20,
            self._t * 12 % 360,
            0,
        )

        # player movement, driven by whatever input source app.py hands us
        speed = 8
        move = Vec3(0, 0, 0)
        if inp.get('w'):
            move.z += 1
        if inp.get('s'):
            move.z -= 1
        if inp.get('a'):
            move.x -= 1
        if inp.get('d'):
            move.x += 1
        if move.length() > 0:
            move = move.normalized()
        self.player.position += move * speed * dt

        if inp.get('space'):
            self._player_vy = 6
        self._player_vy -= 14 * dt
        self.player.y = max(1.2, self.player.y + self._player_vy * dt)
        if self.player.y <= 1.2:
            self._player_vy = 0

        camera.position = self.player.position + Vec3(0, 5, -10)
        camera.look_at(self.player.position + Vec3(0, 1, 0))

        if dt > 0:
            fps = 1 / dt
            self._fps_samples.append(fps)
            if len(self._fps_samples) > 20:
                self._fps_samples.pop(0)
        avg_fps = sum(self._fps_samples) / len(self._fps_samples) if self._fps_samples else 0
        self.info_text.text = f'FPS: {avg_fps:4.1f}   Objects: {self.object_count()}'
