"""
Remote Play demo -- thin viewer.

Runs on the NON-GPU laptop. It does zero 3D rendering: it just decodes the
JPEG frames streamed from the host over the LAN and displays them, and sends
your keypresses back to the host so you're actually controlling the scene
that's rendering on someone else's GPU.

Usage:
    python client.py                        # auto-discovers a host on the LAN
    python client.py 192.168.1.23            # connect to a known IP
    python client.py 192.168.1.23 --code 1234  # skip the code prompt too

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

from shared import FRAME_W, FRAME_H, discover_hosts


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('host', nargs='?', default=None, help='LAN IP of the GPU laptop. Omit to auto-discover.')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--code', default=None, help='6-digit code shown on the host. Omit to be prompted.')
    return p.parse_args()


def resolve_host(args):
    if args.host:
        return args.host, args.port

    print('Searching for GPU hosts on the LAN (3s)...')
    hosts = discover_hosts(timeout=3.0)
    if not hosts:
        print('No hosts found automatically.')
        ip = input('Enter the host IP manually: ').strip()
        return ip, args.port

    if len(hosts) == 1:
        h = hosts[0]
        print(f"Found host: {h.get('name', '?')} @ {h['ip']}")
        return h['ip'], h.get('ws_port', args.port)

    print('Multiple hosts found:')
    for i, h in enumerate(hosts):
        print(f"  [{i}] {h.get('name', '?')} @ {h['ip']}")
    choice = input('Pick a number: ').strip()
    try:
        h = hosts[int(choice)]
        return h['ip'], h.get('ws_port', args.port)
    except (ValueError, IndexError):
        print('Invalid choice.')
        sys.exit(1)


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
    host_ip, port = resolve_host(args)
    code = args.code or input('Enter the 6-digit code shown on the host: ').strip()

    url = f'ws://{host_ip}:{port}'
    print(f'Connecting to {url} ...')
    try:
        ws_ctx = connect(url, max_size=None)
    except Exception as e:
        print(f'Could not connect to host: {e}')
        print('Make sure both laptops are on the same network and app.py --serve is running.')
        sys.exit(1)

    with ws_ctx as ws:
        ws.send(json.dumps({'code': code}))
        try:
            ack = json.loads(ws.recv(timeout=5))
        except Exception:
            print('No response from host during handshake.')
            sys.exit(1)
        if ack.get('status') != 'approved':
            print('Rejected by host - the code was wrong.')
            sys.exit(1)
        print('Code accepted. Streaming should appear in a new window.')

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
