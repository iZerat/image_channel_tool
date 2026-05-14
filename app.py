from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import imageio
import numpy as np
from PIL import Image
import io
import os
import tempfile

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = tempfile.mkdtemp()
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

def normalize_to_rgba(img):
    """Convert any image array to uint8 RGBA"""
    # Handle float images (EXR/HDR)
    if img.dtype in [np.float16, np.float32, np.float64]:
        # Check if data is in 0-1 range or higher
        img_max = np.nanmax(img)
        if img_max > 1.0:
            img = img / img_max
        img = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    elif img.dtype != np.uint8:
        img = img.astype(np.uint8)

    # Ensure RGBA shape
    if img.ndim == 2:
        # Grayscale -> RGBA
        img = np.stack([img, img, img, np.ones_like(img) * 255], axis=-1)
    elif img.shape[-1] == 1:
        # Single channel -> RGBA
        img = np.concatenate([img] * 3 + [np.ones((*img.shape[:2], 1)) * 255], axis=-1)
    elif img.shape[-1] == 3:
        # RGB -> RGBA
        img = np.concatenate([img, np.ones((*img.shape[:2], 1)) * 255], axis=-1)
    elif img.shape[-1] > 4:
        # More than 4 channels, take first 4
        img = img[..., :4]

    return img

def pil_to_buffer(pil_img, format='PNG'):
    """Convert PIL Image to bytes buffer"""
    buf = io.BytesIO()
    pil_img.save(buf, format=format)
    buf.seek(0)
    return buf

@app.route('/api/convert', methods=['POST'])
def convert_image():
    """Convert professional format (EXR/DDS/HDR/TIFF/TGA/etc) to PNG"""
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        file_bytes = file.read()
        print(f"[CONVERT] File: {file.filename}, Size: {len(file_bytes)} bytes")

        img = None
        # Try imageio v2 first (stable API)
        try:
            import imageio.v2 as iio
            img = iio.imread(file_bytes)
            print(f"[CONVERT] imageio v2 success: shape={img.shape}, dtype={img.dtype}")
        except Exception as e1:
            print(f"[CONVERT] imageio v2 failed: {e1}")
            # Fallback to PIL/Pillow
            try:
                pil_img = Image.open(io.BytesIO(file_bytes))
                img = np.array(pil_img)
                print(f"[CONVERT] PIL fallback success: shape={img.shape}, dtype={img.dtype}, mode={pil_img.mode}")
            except Exception as e2:
                print(f"[CONVERT] PIL fallback failed: {e2}")
                return jsonify({'error': f'Cannot read image format. imageio: {e1} | PIL: {e2}'}), 500

        # Validate image data
        if img is None or img.size == 0:
            return jsonify({'error': 'Image data is empty after reading'}), 500

        rgba = normalize_to_rgba(img)
        pil_img = Image.fromarray(rgba)
        buf = pil_to_buffer(pil_img, 'PNG')
        print(f"[CONVERT] Output PNG size: {buf.getbuffer().nbytes} bytes")

        return send_file(
            buf,
            mimetype='image/png',
            as_attachment=False,
            download_name='converted.png'
        )
    except Exception as e:
        print(f"[CONVERT] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Conversion failed: {str(e)}'}), 500

@app.route('/api/split', methods=['POST'])
def split_channels():
    """Split RGBA image into 4 grayscale channel PNGs"""
    if 'image' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400

    file = request.files['image']
    try:
        img = imageio.imread(file.read())
        rgba = normalize_to_rgba(img)

        channels = {}
        channel_names = ['red', 'green', 'blue', 'alpha']

        for i, name in enumerate(channel_names):
            # Extract single channel as grayscale
            gray = rgba[..., i]
            # Create RGBA where R=G=B=channel_value, A=255
            channel_rgba = np.stack([gray, gray, gray, np.ones_like(gray) * 255], axis=-1).astype(np.uint8)
            pil_img = Image.fromarray(channel_rgba)
            buf = pil_to_buffer(pil_img, 'PNG')
            channels[name] = buf

        # Return as multipart or zip - here we return JSON with base64? 
        # Actually for simplicity, return individual files via separate endpoint
        # For now, save to temp and return zip
        import zipfile
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, buf in channels.items():
                zf.writestr(f'{name}-channel.png', buf.getvalue())
        zip_buf.seek(0)

        return send_file(
            zip_buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name='channels.zip'
        )
    except Exception as e:
        return jsonify({'error': f'Split failed: {str(e)}'}), 500

@app.route('/api/combine', methods=['POST'])
def combine_channels():
    """Combine 4 grayscale channel images into RGBA"""
    required = ['red', 'green', 'blue']
    for field in required:
        if field not in request.files:
            return jsonify({'error': f'Missing {field} channel'}), 400

    try:
        channels = {}
        for name in ['red', 'green', 'blue', 'alpha']:
            if name in request.files and request.files[name].filename != '':
                img = imageio.imread(request.files[name].read())
                # Take first channel as intensity
                if img.ndim == 3:
                    channels[name] = img[..., 0]
                else:
                    channels[name] = img
            elif name == 'alpha':
                # Default opaque if not provided
                # Use red channel dimensions as reference
                h, w = channels['red'].shape
                channels['alpha'] = np.ones((h, w), dtype=np.uint8) * 255

        # Ensure all same size
        h, w = channels['red'].shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)

        for i, name in enumerate(['red', 'green', 'blue', 'alpha']):
            if name in channels:
                # Resize if needed
                ch = channels[name]
                if ch.shape != (h, w):
                    pil_ch = Image.fromarray(ch).resize((w, h), Image.LANCZOS)
                    ch = np.array(pil_ch)
                rgba[..., i] = ch

        pil_img = Image.fromarray(rgba)
        buf = pil_to_buffer(pil_img, 'PNG')

        return send_file(
            buf,
            mimetype='image/png',
            as_attachment=True,
            download_name='combined-rgba.png'
        )
    except Exception as e:
        return jsonify({'error': f'Combine failed: {str(e)}'}), 500

@app.route('/api/info', methods=['POST'])
def image_info():
    """Get image metadata (dimensions, channels, bit depth, format)"""
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

        # Determine channel count
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
    print("  POST /api/split    - Split RGBA to channel ZIP")
    print("  POST /api/combine  - Combine channels to RGBA")
    print("  POST /api/info     - Get image metadata")
    print("  GET  /api/health   - Health check")
    app.run(host='0.0.0.0', port=5000, debug=True)