import os
from PIL import Image
import numpy as np
import random
import torch
from torch.utils.data import Dataset
import torch.nn.functional as F
import torchvision.transforms as T
from torchvision.transforms.functional import InterpolationMode
from transformers import CLIPVisionModel, CLIPVisionModelWithProjection
from diffusers import AutoencoderKL


def spherical_dist_loss(x, y):
    # TODO: change 768 in accordance with new embeds
    x = F.normalize(x, dim=-1)  # 4 cuts -> shape = (4,1,768)
    y = F.normalize(y, dim=-1)  # text embed always (1,768)
    return (x - y).norm(dim=-1).div(2).arcsin().pow(2).mul(2)


def save_progress(text_encoder, placeholder_token_id, accelerator, args, save_path, subtokens):
    if isinstance(placeholder_token_id, list):
        learned_embeds = accelerator.unwrap_model(text_encoder).get_input_embeddings().weight[placeholder_token_id]
        sd_version = "XL"
        learned_embeds_dict = {"placeholder_token": args.placeholder_token,
                               "subtokens": {},
                               "SD_version": sd_version}
        for i, tok in enumerate(subtokens):
            learned_embeds_dict["subtokens"][tok] = learned_embeds[i].detach().cpu()
        torch.save(learned_embeds_dict, save_path)
    else:
        learned_embeds = accelerator.unwrap_model(text_encoder).get_input_embeddings().weight[placeholder_token_id]
        # TODO i imagine we're going to be using custom models with this so will need a better solution
        sd_version = "XL"
        learned_embeds_dict = {"placeholder_token": args.placeholder_token,
                               "embedding": learned_embeds.detach().cpu(),
                               "SD_version": sd_version}
        torch.save(learned_embeds_dict, save_path)


PIL_INTERPOLATION = {
    "linear": Image.Resampling.BILINEAR,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
    "nearest": Image.Resampling.NEAREST,
}

imagenet_templates_small = [
    "a photo of a {}",
    "a rendering of a {}",
    "a cropped photo of the {}",
    "the photo of a {}",
    "a photo of a clean {}",
    "a photo of a dirty {}",
    "a dark photo of the {}",
    "a photo of my {}",
    "a photo of the cool {}",
    "a close-up photo of a {}",
    "a bright photo of the {}",
    "a cropped photo of a {}",
    "a photo of the {}",
    "a good photo of the {}",
    "a photo of one {}",
    "a close-up photo of the {}",
    "a rendition of the {}",
    "a photo of the clean {}",
    "a rendition of a {}",
    "a photo of a nice {}",
    "a good photo of a {}",
    "a photo of the nice {}",
    "a photo of the small {}",
    "a photo of the weird {}",
    "a photo of the large {}",
    "a photo of a cool {}",
    "a photo of a small {}",
]

imagenet_style_templates_small = [
    "a painting in the style of {}",
    "a rendering in the style of {}",
    "a cropped painting in the style of {}",
    "the painting in the style of {}",
    "a clean painting in the style of {}",
    "a dirty painting in the style of {}",
    "a dark painting in the style of {}",
    "a picture in the style of {}",
    "a cool painting in the style of {}",
    "a close-up painting in the style of {}",
    "a bright painting in the style of {}",
    "a cropped painting in the style of {}",
    "a good painting in the style of {}",
    "a close-up painting in the style of {}",
    "a rendition in the style of {}",
    "a nice painting in the style of {}",
    "a small painting in the style of {}",
    "a weird painting in the style of {}",
    "a large painting in the style of {}",
]

blank_template = ["{}"]

# TODO: what?
preprocess_clip = T.Compose([
    T.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711])
])
resize224 = T.Resize(224, interpolation=InterpolationMode.BICUBIC, antialias=True)


def sliding_cutouts(image, num_cuts=4, cut_size=224):
    image_tensor = preprocess_clip(image)
    image_tensor = resize224(image_tensor)

    cutouts = []
    sideY, sideX = image_tensor.shape[2:4]
    largersize = max(sideX, sideY)
    if sideX < sideY:
        smaller_side = "sideX"
    else:
        smaller_side = "sideY"

    cut_spots = torch.linspace(0, largersize - 224, num_cuts)
    for cut in cut_spots:
        if smaller_side == "sideX":
            cutout = image_tensor[:, :, int(cut):int(cut) + 224, :]
        else:
            cutout = image_tensor[:, :, :, int(cut):int(cut) + 224]
        cutouts.append(
            F.interpolate(cutout, size=(cut_size, cut_size), mode='bicubic', align_corners=False, antialias=True))
        del cutout

    cutouts = torch.cat(cutouts, dim=0)
    return cutouts


class TextualInversionDataset(Dataset):
    # noinspection PyTypeChecker
    def __init__(
            self,
            data_root,
            tokenizer,
            learnable_property="object",  # [object, style]
            size=512,
            repeats=100,
            interpolation="bicubic",
            flip_p=0.5,
            set_type="train",
            placeholder_token="*",
            center_crop=False,
            vision_model_path=None,
            initializer_token="*",
            custom_prompts=False,
            pad_tokens=True,
            subtokens=None,
            vae_path=None,
    ):
        self.data_root = data_root
        self.tokenizer = tokenizer
        self.learnable_property = learnable_property
        self.size = size
        self.placeholder_token = placeholder_token
        self.center_crop = center_crop
        self.flip_p = flip_p
        self.initializer_token = initializer_token
        self.vision_model_path = vision_model_path
        self.pad_tokens = pad_tokens
        self.subtokens = subtokens
        if subtokens is not None:
            self.num_vectors = len(subtokens)
        else:
            self.num_vectors = 1

        #  TODO need better way to recognize picture extension
        self.image_paths = [os.path.join(self.data_root, file_path) for file_path in os.listdir(self.data_root)
                            if file_path.endswith(".jpeg") or file_path.endswith(".png") or file_path.endswith(".jpg")]
        if custom_prompts:
            # check if there are text files to pull from
            txt_file_exists = False
            all_files = os.listdir(self.data_root)
            for file_path in all_files:
                if file_path.endswith(".txt"):
                    txt_file_exists = True
                    break
            if txt_file_exists:
                captions = []
                for file_path in os.listdir(self.data_root):
                    if file_path.endswith(".txt"):
                        with open(os.path.join(self.data_root, file_path), 'r') as f:
                            captions.append(f.readline())
                self.captions = captions
            else:
                self.captions = [file_path.split(".")[0] for file_path in os.listdir(self.data_root)
                                 if file_path.endswith(".jpeg") or file_path.endswith(".png")
                                 or file_path.endswith(".jpg")]

        else:
            self.captions = None

        self.num_images = len(self.image_paths)
        self._length = self.num_images

        if set_type == "train":
            self._length = self.num_images * repeats

        self.interpolation = {
            "linear": PIL_INTERPOLATION["linear"],
            "bilinear": PIL_INTERPOLATION["bilinear"],
            "bicubic": PIL_INTERPOLATION["bicubic"],
            "lanczos": PIL_INTERPOLATION["lanczos"],
        }[interpolation]

        self.templates = imagenet_style_templates_small if learnable_property == "style" else imagenet_templates_small \
            if learnable_property == "object" else blank_template
        self.flip_transform = T.RandomHorizontalFlip(p=self.flip_p)

        image_model = CLIPVisionModelWithProjection.from_pretrained(vision_model_path).to("cuda").to(torch.float16).eval()

        with torch.no_grad():
            self.clip_embeddings = []
            for path in self.image_paths:
                image = Image.open(path).convert("RGB")
                image = self.flip_transform(image)
                image = np.array(image).astype(np.float32) / 255.0
                image = image[None].transpose(0, 3, 1, 2)
                image = torch.from_numpy(image).to("cuda").to(torch.float16)
                cuts = sliding_cutouts(image, num_cuts=4, cut_size=224)
                embeds = image_model(cuts).image_embeds.mean(dim=0)
                self.clip_embeddings.append(embeds)

            self.initializer_token_id = self.tokenizer(initializer_token).input_ids[1]  # ignore start and end token

        if vae_path is not None:
            with torch.no_grad():
                vae = AutoencoderKL.from_pretrained(vae_path, subfolder="vae").to("cuda").to(torch.float16).eval()
                self.latents = []
                for path in self.image_paths:
                    image = Image.open(path).convert("RGB")
                    img = np.array(image).astype(np.uint8)

                    if self.center_crop:
                        crop = min(img.shape[0], img.shape[1])
                        (
                            h,
                            w,
                        ) = (
                            img.shape[0],
                            img.shape[1],
                        )
                        img = img[(h - crop) // 2: (h + crop) // 2, (w - crop) // 2: (w + crop) // 2]

                    image = Image.fromarray(img)
                    image = image.resize((self.size, self.size), resample=self.interpolation)

                    image = self.flip_transform(image)
                    image = np.array(image).astype(np.uint8)
                    image = (image / 127.5 - 1.0).astype(np.float32)
                    image = torch.from_numpy(image).permute(2, 0, 1).to(torch.float16).to("cuda").unsqueeze(0)
                    latents = vae.encode(image).latent_dist.sample().squeeze(0)
                    latents = latents * 0.13025
                    self.latents.append(latents)
        else:
            self.latents = None

    def __len__(self):
        return self._length

    # noinspection PyTypeChecker
    def __getitem__(self, i):
        if self.vision_model_path is not None:
            example = {}

            if self.captions is None:
                if self.num_vectors > 1:
                    assert isinstance(self.subtokens, list)
                    text = random.choice(self.templates)
                    begin, end = text.replace("{", "").split("}")
                    newstr = ""
                    for tok in self.subtokens:
                        newstr = newstr + tok + " "
                    text = newstr + end
                else:
                    text = random.choice(self.templates).format(self.placeholder_token)
            else:
                text = self.captions[i % self.num_images]

            example["input_ids"] = self.tokenizer(
                text,
                padding="max_length" if self.pad_tokens else "do_not_pad",
                truncation=True,
                max_length=self.tokenizer.model_max_length,
                return_tensors="pt",
            ).input_ids[0]

            # if self.what_to_train != "layer":
            # example["token_position"] = torch.where(example["input_ids"] == self.initializer_token_id)[0][0].item()

            example["clip_embeds"] = self.clip_embeddings[i % self.num_images]
            return example

        else:
            example = {}

            if self.latents is None:
                text = random.choice(self.templates).format(self.placeholder_token)

                example["input_ids"] = self.tokenizer(
                    text,
                    padding="max_length" if self.pad_tokens else "do_not_pad",
                    truncation=True,
                    max_length=self.tokenizer.model_max_length,
                    return_tensors="pt",
                ).input_ids[0]

                image = Image.open(self.image_paths[i % self.num_images])

                if not image.mode == "RGB":
                    image = image.convert("RGB")

                # default to score-sde preprocessing
                img = np.array(image).astype(np.uint8)

                if self.center_crop:
                    crop = min(img.shape[0], img.shape[1])
                    (
                        h,
                        w,
                    ) = (
                        img.shape[0],
                        img.shape[1],
                    )
                    img = img[(h - crop) // 2: (h + crop) // 2, (w - crop) // 2: (w + crop) // 2]

                image = Image.fromarray(img)
                image = image.resize((self.size, self.size), resample=self.interpolation)

                image = self.flip_transform(image)
                image = np.array(image).astype(np.uint8)
                image = (image / 127.5 - 1.0).astype(np.float32)
                example["pixel_values"] = torch.from_numpy(image).permute(2, 0, 1)
            else:
                if self.captions is None:
                    if self.num_vectors > 1:
                        assert isinstance(self.subtokens, list)
                        text = random.choice(self.templates)
                        begin, end = text.replace("{", "").split("}")
                        newstr = ""
                        for tok in self.subtokens:
                            newstr = newstr + tok + " "
                        text = newstr + end
                    else:
                        text = random.choice(self.templates).format(self.placeholder_token)
                else:
                    text = self.captions[i % self.num_images]

                example["input_ids"] = self.tokenizer(
                    text,
                    padding="max_length" if self.pad_tokens else "do_not_pad",
                    truncation=True,
                    max_length=self.tokenizer.model_max_length,
                    return_tensors="pt",
                ).input_ids[0]

                example["latents"] = self.latents[i % self.num_images]

            return example
