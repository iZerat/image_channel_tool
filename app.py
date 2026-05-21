from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import imageio
import numpy as np
from PIL import Image
import io
import os
import tempfile
import base64

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = tempfile.mkdtemp()
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

def normalize_to_rgba(img):
    """Convert any image array to uint8 RGBA"""
    img = np.asarray(img)
    if img.dtype in [np.float16, np.float32, np.float64]:
        img_max = np.nanmax(img)
        if img_max > 1.0:
            img = img / img_max
        img = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    elif img.dtype != np.uint8:
        img = img.astype(np.uint8)

    if img.ndim == 2:
        img = np.stack([img, img, img, np.ones_like(img) * 255], axis=-1)
    elif img.ndim == 3:
        if img.shape[-1] == 1:
            img = np.concatenate([img] * 3 + [np.ones((*img.shape[:2], 1)) * 255], axis=-1)
        elif img.shape[-1] == 3:
            img = np.concatenate([img, np.ones((*img.shape[:2], 1)) * 255], axis=-1)
        elif img.shape[-1] > 4:
            img = img[..., :4]
    else:
        raise ValueError(f"Unsupported image dimensions: {img.ndim}, shape={img.shape}")

    img = np.ascontiguousarray(img, dtype=np.uint8)
    return img

def pil_to_buffer(pil_img, format='PNG'):
    buf = io.BytesIO()
    pil_img.save(buf, format=format)
    buf.seek(0)
    return buf

def img_to_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')

@app.route('/api/convert', methods=['POST'])
def convert_image():
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    try:
        file_bytes = file.read()
        print(f"[CONVERT] File: {file.filename}, Size: {len(file_bytes)} bytes")
        img = None
        try:
            import imageio.v2 as iio
            img = iio.imread(file_bytes)
            print(f"[CONVERT] imageio v2 success: shape={img.shape}, dtype={img.dtype}")
        except Exception as e1:
            print(f"[CONVERT] imageio v2 failed: {e1}")
            try:
                pil_img = Image.open(io.BytesIO(file_bytes))
                img = np.array(pil_img)
                print(f"[CONVERT] PIL fallback success: shape={img.shape}, dtype={img.dtype}, mode={pil_img.mode}")
            except Exception as e2:
                print(f"[CONVERT] PIL fallback failed: {e2}")
                return jsonify({'error': f'Cannot read image format. imageio: {e1} | PIL: {e2}'}), 500
        if img is None or img.size == 0:
            return jsonify({'error': 'Image data is empty after reading'}), 500
        rgba = normalize_to_rgba(img)
        pil_img = Image.fromarray(rgba)
        buf = pil_to_buffer(pil_img, 'PNG')
        print(f"[CONVERT] Output PNG size: {buf.getbuffer().nbytes} bytes")
        return send_file(buf, mimetype='image/png', as_attachment=False, download_name='converted.png')
    except Exception as e:
        print(f"[CONVERT] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Conversion failed: {str(e)}'}), 500

@app.route('/api/split', methods=['POST'])
def split_channels():
    """Split RGBA image into 4 grayscale channel PNGs + RGB Combined, return as base64 JSON"""
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    file = request.files['image']
    try:
        # Use PIL directly to read original file
        # Channel Packed PNG: RGB and Alpha are independent channels
        # Do NOT unpremultiply - the RGB values are the actual color data
        pil_img = Image.open(file.stream)
        if pil_img.mode != 'RGBA':
            pil_img = pil_img.convert('RGBA')
        rgba = np.array(pil_img)

        h, w = rgba.shape[:2]
        channels = {}
        channel_names = ['red', 'green', 'blue', 'alpha']

        for i, name in enumerate(channel_names):
            gray = rgba[..., i]
            channel_rgba = np.stack([gray, gray, gray, np.ones_like(gray) * 255], axis=-1).astype(np.uint8)
            channels[name] = img_to_base64(Image.fromarray(channel_rgba))

        # RGB Combined: display RGB channel directly, ignore alpha for preview
        # Channel Packed: RGB contains the actual image, Alpha is a separate mask
        rgb_combined = rgba.copy()
        rgb_combined[..., 3] = 255  # Force opaque so browser shows RGB
        channels['rgb'] = img_to_base64(Image.fromarray(rgb_combined))

        has_alpha = bool(np.any(rgba[..., 3] != 255))

        return jsonify({
            'success': True,
            'channels': channels,
            'width': int(w),
            'height': int(h),
            'hasAlpha': has_alpha
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Split failed: {str(e)}'}), 500

@app.route('/api/combine', methods=['POST'])
def combine_channels():
    required = ['red', 'green', 'blue']
    for field in required:
        if field not in request.files:
            return jsonify({'error': f'Missing {field} channel'}), 400
    try:
        channels = {}
        for name in ['red', 'green', 'blue', 'alpha']:
            if name in request.files and request.files[name].filename != '':
                img = imageio.imread(request.files[name].read())
                if img.ndim == 3:
                    channels[name] = img[..., 0]
                else:
                    channels[name] = img
            elif name == 'alpha':
                h, w = channels['red'].shape
                channels['alpha'] = np.ones((h, w), dtype=np.uint8) * 255

        h, w = channels['red'].shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        for i, name in enumerate(['red', 'green', 'blue', 'alpha']):
            if name in channels:
                ch = channels[name]
                if ch.shape != (h, w):
                    pil_ch = Image.fromarray(ch).resize((w, h), Image.LANCZOS)
                    ch = np.array(pil_ch)
                rgba[..., i] = ch

        pil_img = Image.fromarray(rgba)
        buf = pil_to_buffer(pil_img, 'PNG')
        return send_file(buf, mimetype='image/png', as_attachment=True, download_name='combined-rgba.png')
    except Exception as e:
        return jsonify({'error': f'Combine failed: {str(e)}'}), 500

@app.route('/api/info', methods=['POST'])
def image_info():
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    file = request.files['image']
    try:
        img = imageio.imread(file.read())
        info = {
            'shape': list(img.shape),
            'dtype': str(img.dtype),
            'ndim': img.ndim,
            'min': float(np.nanmin(img)),
            'max': float(np.nanmax(img)),
            'mean': float(np.nanmean(img))
        }
        if img.ndim == 2:
            info['channels'] = 1
            info['channel_names'] = ['Luminance']
        elif img.ndim == 3:
            info['channels'] = img.shape[-1]
            if img.shape[-1] == 3:
                info['channel_names'] = ['R', 'G', 'B']
            elif img.shape[-1] == 4:
                info['channel_names'] = ['R', 'G', 'B', 'A']
            else:
                info['channel_names'] = [f'Ch{i+1}' for i in range(img.shape[-1])]
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'formats': ['EXR', 'DDS', 'HDR', 'TIFF', 'PNG', 'JPG', 'TGA', 'BMP', 'GIF']})

if __name__ == '__main__':
    print(f"Upload folder: {UPLOAD_FOLDER}")
    print("Supported formats: EXR, DDS, HDR, TIFF, PNG, JPG, TGA, BMP, GIF")
    print("API endpoints:")
    print("  POST /api/convert  - Convert any format to PNG")
    print("  POST /api/split    - Split RGBA to channel JSON (base64)")
    print("  POST /api/combine  - Combine channels to RGBA")
    print("  POST /api/info     - Get image metadata")
    print("  GET  /api/health   - Health check")
    app.run(host='0.0.0.0', port=5000, debug=True)