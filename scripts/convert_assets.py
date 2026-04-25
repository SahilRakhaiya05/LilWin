
import os
import subprocess

def convert_to_png_sequence(video_path, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Use ffmpeg to extract frames with alpha channel
    # -i input
    # -vsync 0 (keep all frames)
    # output path with pattern
    command = [
        "ffmpeg",
        "-i", video_path,
        "-vsync", "0",
        os.path.join(output_dir, "frame%04d.png")
    ]
    subprocess.run(command, check=True)

if __name__ == "__main__":
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets_dir = os.path.join(repo_root, "src", "assets")
    
    # Bruce
    convert_to_png_sequence(
        os.path.join(assets_dir, "walk-bruce-01.mov"),
        os.path.join(assets_dir, "bruce_frames")
    )
    
    # Jazz
    convert_to_png_sequence(
        os.path.join(assets_dir, "walk-jazz-01.mov"),
        os.path.join(assets_dir, "jazz_frames")
    )
