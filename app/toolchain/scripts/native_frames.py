"""Timestamp-checked, settled native preview frames; no external rendering."""
import base64
import struct
import time

from native_service import NativeServiceError


def remaining_seconds(deadline):
    remaining = deadline - time.monotonic()
    if remaining < 0.001:
        raise NativeServiceError('Native preview settling deadline exceeded')
    return remaining


def bounded_request(service, session, operation, params, deadline):
    """Tighten this serial service call to the remaining settling budget.

    Service.request has no per-call timeout argument. Temporarily lower its
    existing request_timeout and always restore it. Its subprocess cleanup
    guard can take up to five extra seconds; a late result is never accepted.
    This helper, like the owning Service session, must be used serially.
    """
    timeout = service.request_timeout
    service.request_timeout = min(timeout, remaining_seconds(deadline))
    try:
        result = service.request('player', operation, params, session)
    finally:
        service.request_timeout = timeout
    remaining_seconds(deadline)
    return result


def read_frame(service, session, width, height, deadline=None):
    params = {'width': width, 'height': height}
    response = (service.request('player', 'captureFrame', params, session) if deadline is None
                else bounded_request(service, session, 'captureFrame', params, deadline))
    result = response['result']
    packet = base64.b64decode(result['packetBase64'], validate=True)
    if len(packet) < 40 or packet[:4] != b'VEF1':
        raise NativeServiceError('Unknown native frame header')
    w, h, stride, fmt = struct.unpack_from('<IIII', packet, 8)
    pts = struct.unpack_from('<q', packet, 24)[0]
    if w != width or h != height or fmt != 1 or stride < w*4 or len(packet) != 40 + stride*h:
        raise NativeServiceError('Unknown native frame layout')
    pixels = b''.join(packet[40+y*stride:40+y*stride+w*4] for y in range(h))
    if deadline is not None:
        remaining_seconds(deadline)
    return (w, h, pixels), pts


def prime(service, session, duration, width, height):
    # This build can expose initial pixels before fixed-canvas layout settles.
    # Seek away from frame zero and discard three native frames, as verified by
    # the variable-alpha test. Do not present these warm-up frames as output.
    when = min(500000, duration // 2)
    deadline, count = time.monotonic() + 5, 0
    bounded_request(service, session, 'seek', {'positionUs': when, 'seekMode': 'done'}, deadline)
    while count < 3:
        try:
            read_frame(service, session, width, height, deadline)
            count += 1
        except NativeServiceError as exc:
            if 'frame_unavailable' not in str(exc):
                raise
        remaining = remaining_seconds(deadline)
        if count < 3:
            time.sleep(min(0.1, remaining))


def capture_at(service, session, position_us, width, height, tolerance_us=40000):
    deadline = time.monotonic() + 5
    bounded_request(service, session, 'seek', {'positionUs': position_us, 'seekMode': 'done'}, deadline)
    previous, stable = None, 0
    while True:
        try:
            frame, pts = read_frame(service, session, width, height, deadline)
            if abs(pts-position_us) <= tolerance_us:
                stable = stable+1 if frame == previous else 1
                previous = frame
                if stable >= 3:
                    remaining_seconds(deadline)
                    return frame, pts
            else:
                previous, stable = None, 0
        except NativeServiceError as exc:
            if 'frame_unavailable' not in str(exc):
                raise
            previous, stable = None, 0
        time.sleep(min(0.1, remaining_seconds(deadline)))
