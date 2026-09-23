"""
Remote Play demo -- thin viewer.

Runs on the NON-GPU laptop. It does zero 3D rendering: it just decodes the
JPEG frames streamed from the host over the LAN and displays them, and sends
your keypresses back to the host so you're actually controlling the scene
that's rendering on someone else's GPU.

Usage:
    python client.py <host_ip> [--port 8765]

Controls: W A S D to move, Space to float up, E to spawn more objects
(more GPU load on the host), Esc or close the window to quit.
"""

import argparse
import json
import sys
import threading

import cv2
import numpy as np
import pygame
from websockets.sync.client import connect

from shared import FRAME_W, FRAME_H


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('host', help='LAN IP address of the GPU laptop running app.py --serve')
    p.add_argument('--port', type=int, default=8765)
    return p.parse_args()


def receiver_loop(ws, state, state_lock):
    try:
        for message in ws:
            arr = np.frombuffer(message, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            surface = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
            with state_lock:
                state['surface'] = surface
    except Exception as e:
        with state_lock:
            state['error'] = str(e)


def main():
    args = parse_args()
    url = f'ws://{args.host}:{args.port}'
    print(f'Connecting to {url} ...')
    try:
        ws_ctx = connect(url, max_size=None)
    except Exception as e:
        print(f'Could not connect to host: {e}')
        print('Make sure both laptops are on the same network and app.py --serve is running.')
        sys.exit(1)

    with ws_ctx as ws:
        print('Connected. Streaming should appear in a new window.')

        pygame.init()
        screen = pygame.display.set_mode((FRAME_W, FRAME_H))
        pygame.display.set_caption('Remote Play - Viewer (streamed from GPU host, no local rendering)')
        clock = pygame.time.Clock()
        font = pygame.font.SysFont('consolas', 18)

        state = {'surface': None, 'error': None}
        state_lock = threading.Lock()
        threading.Thread(target=receiver_loop, args=(ws, state, state_lock), daemon=True).start()

        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_e:
                    try:
                        ws.send(json.dumps({'spawn': True}))
                    except Exception:
                        pass

            keys = pygame.key.get_pressed()
            payload = {
                'w': keys[pygame.K_w],
                'a': keys[pygame.K_a],
                's': keys[pygame.K_s],
                'd': keys[pygame.K_d],
                'space': keys[pygame.K_SPACE],
            }
            try:
                ws.send(json.dumps(payload))
            except Exception:
                running = False

            with state_lock:
                surface, err = state['surface'], state['error']
            if err:
                print(f'Connection lost: {err}')
                running = False
            if surface is not None:
                if surface.get_size() != (FRAME_W, FRAME_H):
                    surface = pygame.transform.scale(surface, (FRAME_W, FRAME_H))
                screen.blit(surface, (0, 0))
                hint = font.render('LIVE from GPU host - this laptop is only decoding video', True, (255, 255, 0))
                screen.blit(hint, (10, FRAME_H - 26))
            pygame.display.flip()
            clock.tick(60)

    pygame.quit()


if __name__ == '__main__':
    main()
