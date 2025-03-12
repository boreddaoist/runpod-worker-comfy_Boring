import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
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
    """
    Validates the input for the handler function.
    
    Returns:
        tuple: (validated_data, error_message)
    """
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
    """
    Check if ComfyUI API is reachable
    Returns: bool - True if server is ready
    """
    for i in range(retries):
        try:
            if requests.get(url).status_code == 200:
                logging.info("ComfyUI API is reachable")
                return True
        except requests.RequestException:
            pass
        time.sleep(delay / 1000)
    logging.error(f"Failed to connect to ComfyUI at {url} after {retries} attempts")
    return False


def upload_images(images):
    """Upload base64 encoded images to ComfyUI"""
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []
    logging.info("Starting image upload")

    for image in images:
        name = image["name"]
        blob = base64.b64decode(image["image"])
        files = {
            "image": (name, BytesIO(blob), "image/png"),
            "overwrite": (None, "true"),
        }
        try:
            response = requests.post(f"http://{COMFY_HOST}/upload/image", files=files)
            if response.status_code == 200:
                responses.append(f"Uploaded {name}")
            else:
                upload_errors.append(f"{name}: {response.text}")
        except Exception as e:
            upload_errors.append(f"{name}: {str(e)}")

    if upload_errors:
        logging.error(f"Image upload completed with {len(upload_errors)} errors")
        return {
            "status": "error",
            "message": "Partial upload failure",
            "details": upload_errors,
        }

    logging.info("All images uploaded successfully")
    return {
        "status": "success",
        "message": "All images uploaded",
        "details": responses,
    }


def queue_workflow(workflow):
    """Queue workflow with ComfyUI"""
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(f"http://{COMFY_HOST}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    """Retrieve workflow execution history"""
    with urllib.request.urlopen(f"http://{COMFY_HOST}/history/{prompt_id}") as response:
        return json.loads(response.read())


def base64_encode(img_path):
    """Encode image file to base64 string"""
    with open(img_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def process_output_images(outputs, job_id):
    COMFY_OUTPUT_PATH = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")
    
    # 1. First check for videos in root output directory
    all_files = os.listdir(COMFY_OUTPUT_PATH)
    video_files = [f for f in all_files if f.startswith("LP_") and f.endswith(".mp4")]
    
    if video_files:
        # Get most recent video
        video_files.sort(key=lambda x: os.path.getctime(os.path.join(COMFY_OUTPUT_PATH, x)))
        video_path = os.path.join(COMFY_OUTPUT_PATH, video_files[-1])
        
        try:
            if os.environ.get("BUCKET_ENDPOINT_URL"):
                video_url = rp_upload.upload_file(job_id, video_path)
                return {"status": "success", "message": video_url, "is_video": True}
            else:
                with open(video_path, "rb") as f:
                    return {
                        "status": "success",
                        "message": base64.b64encode(f.read()).decode("utf-8"),
                        "is_video": True
                    }
        except Exception as e:
            return {"status": "error", "message": f"Video handling failed: {str(e)}"}

    # Image handling fallback
    output_images = {}
    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for image in node_output["images"]:
                img_path = os.path.join(image["subfolder"], image["filename"])
                output_images = img_path

    local_image_path = os.path.join(COMFY_OUTPUT_PATH, output_images)
    if os.path.exists(local_image_path):
        logging.info(f"Processing image output: {local_image_path}")
        try:
            if os.environ.get("BUCKET_ENDPOINT_URL"):
                return {"status": "success", "message": rp_upload.upload_image(job_id, local_image_path)}
            return {"status": "success", "message": base64_encode(local_image_path)}
        except Exception as e:
            logging.error(f"Image processing failed: {str(e)}")
            return {"status": "error", "message": str(e)}
    
    return {"status": "error", "message": "No output files generated"}


def handler(job):
    """Main job handler function"""
    job_input = job["input"]
    
    # Validate input
    validated_data, error = validate_input(job_input)
    if error:
        return {"error": error}

    # Verify API availability
    if not check_server(
        f"http://{COMFY_HOST}",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {"error": "ComfyUI API unavailable"}

    # Upload input images
    upload_result = upload_images(validated_data.get("images", []))
    if upload_result["status"] == "error":
        return upload_result

    # Queue workflow
    try:
        queued = queue_workflow(validated_data["workflow"])
        prompt_id = queued["prompt_id"]
        logging.info(f"Queued workflow ID: {prompt_id}")
    except Exception as e:
        return {"error": f"Workflow queueing failed: {str(e)}"}

    # Poll for completion
    logging.info("Starting output polling")
    start_time = time.time()
    try:
        while (time.time() - start_time) < (COMFY_POLLING_MAX_RETRIES * COMFY_POLLING_INTERVAL_MS / 1000):
            history = get_history(prompt_id)
            if prompt_id in history and history[prompt_id].get("outputs"):
                break
            time.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
        else:
            return {"error": "Workflow execution timeout"}
    except Exception as e:
        return {"error": f"Polling failed: {str(e)}"}

    # Process outputs
    result = process_output_images(history[prompt_id].get("outputs", {}), job["id"])
    
    # Cleanup
    try:
        output_dir = os.environ.get("COMFY_OUTPUT_PATH", "/comfyui/output")
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                os.remove(os.path.join(root, f))
        logging.info("Output directory cleaned")
    except Exception as e:
        logging.warning(f"Cleanup error: {str(e)}")

    return {**result, "refresh_worker": REFRESH_WORKER}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
