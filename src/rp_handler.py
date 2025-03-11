import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 50
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 500
# Time to wait between poll attempts in milliseconds
COMFY_POLLING_INTERVAL_MS = int(os.environ.get("COMFY_POLLING_INTERVAL_MS", 250))
# Maximum number of poll attempts
COMFY_POLLING_MAX_RETRIES = int(os.environ.get("COMFY_POLLING_MAX_RETRIES", 500))
# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"


def validate_input(job_input):
    """Validates the input for the handler function."""
    if job_input is None:
        return None, "Please provide input"

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "image" in image for image in images
        ):
            return (
                None,
                "'images' must be a list of objects with 'name' and 'image' keys",
            )

    return {"workflow": workflow, "images": images}, None


def check_server(url, retries=500, delay=50):
    """Check if server is reachable via HTTP GET request"""
    for i in range(retries):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                print(f"runpod-worker-comfy - API is reachable")
                return True
        except requests.RequestException:
            pass
        time.sleep(delay / 1000)
    print(f"runpod-worker-comfy - Failed to connect to server at {url} after {retries} attempts.")
    return False


def upload_images(images):
    """Upload images to ComfyUI server"""
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []
    print(f"runpod-worker-comfy - image(s) upload")

    for image in images:
        name = image["name"]
        image_data = image["image"]
        blob = base64.b64decode(image_data)
        files = {
            "image": (name, BytesIO(blob), "image/png"),
            "overwrite": (None, "true"),
        }
        response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files)
        if response.status_code != 200:
            upload_errors.append(f"Error uploading {name}: {response.text}")
        else:
            responses.append(f"Successfully uploaded {name}")

    if upload_errors:
        print(f"runpod-worker-comfy - image(s) upload with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    print(f"runpod-worker-comfy - image(s) upload complete")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }


def queue_workflow(workflow):
    """Queue workflow to be processed by ComfyUI"""
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(f"http://{COMFY_HOST}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    """Retrieve prompt history from ComfyUI"""
    with urllib.request.urlopen(f"http://{COMFY_HOST}/history/{prompt_id}") as response:
        return json.loads(response.read())


def base64_encode(img_path):
    """Return base64 encoded image"""
    with open(img_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def process_output_videos(outputs, job_id):
    """Process video outputs from VHS_VideoCombine node"""
    COMFY_OUTPUT_PATH = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")
    video_path = os.path.join(COMFY_OUTPUT_PATH, "LP.mp4")
    
    print(f"Checking for video at: {video_path}")
    if not os.path.exists(video_path):
        print("No video file found")
        return None

    # Handle S3 upload
    if os.environ.get("BUCKET_ENDPOINT_URL"):
        video_url = rp_upload.upload_file(job_id, video_path)
        print(f"Video uploaded to S3: {video_url}")
        return {"status": "success", "message": video_url}
    
    # Handle base64 encoding
    video_size = os.path.getsize(video_path)
    if video_size > 20 * 1024 * 1024:
        return {"error": "Video exceeds 20MB limit. Configure S3 bucket."}
    
    with open(video_path, "rb") as f:
        return {
            "status": "success",
            "message": base64.b64encode(f.read()).decode("utf-8"),
            "mime_type": "video/mp4"
        }


def process_output_images(outputs, job_id):
    """Process outputs with video priority"""
    # Check for video first
    video_result = process_output_videos(outputs, job_id)
    if video_result:
        print("Video output processed")
        if "error" in video_result:
            return video_result
        return {**video_result, "refresh_worker": REFRESH_WORKER}
    
    # Fall back to image processing
    COMFY_OUTPUT_PATH = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")
    output_images = {}

    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for image in node_output["images"]:
                output_images = os.path.join(image["subfolder"], image["filename"])

    print(f"runpod-worker-comfy - image generation is done")
    local_image_path = f"{COMFY_OUTPUT_PATH}/{output_images}"
    print(f"Checking image path: {local_image_path}")

    if os.path.exists(local_image_path):
        if os.environ.get("BUCKET_ENDPOINT_URL"):
            image = rp_upload.upload_image(job_id, local_image_path)
            print("Image uploaded to S3")
        else:
            image = base64_encode(local_image_path)
            print("Image converted to base64")
        return {"status": "success", "message": image}
    else:
        print("No output file found")
        return {
            "status": "error",
            "message": f"Output file not found: {local_image_path}",
        }


def handler(job):
    """Main handler function"""
    job_input = job["input"]
    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow = validated_data["workflow"]
    images = validated_data.get("images")

    check_server(
        f"http://{COMFY_HOST}",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    )

    upload_result = upload_images(images)
    if upload_result["status"] == "error":
        return upload_result

    try:
        queued_workflow = queue_workflow(workflow)
        prompt_id = queued_workflow["prompt_id"]
        print(f"Queued workflow with ID: {prompt_id}")
    except Exception as e:
        return {"error": f"Error queuing workflow: {str(e)}"}

    retries = 0
    try:
        while retries < COMFY_POLLING_MAX_RETRIES:
            history = get_history(prompt_id)
            if prompt_id in history and history[prompt_id].get("outputs"):
                break
            time.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
            retries += 1
        else:
            return {"error": "Max retries reached"}
    except Exception as e:
        return {"error": f"Error during polling: {str(e)}"}

    result = process_output_images(history[prompt_id].get("outputs"), job["id"])
    return {**result, "refresh_worker": REFRESH_WORKER}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
