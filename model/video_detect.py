from .utils.getter import *
import os
import cv2
import torch
from torch.utils.data import Dataset, DataLoader
from utils.utils import draw_boxes_v2, write_to_video
from .utils.postprocess import postprocessing
from tqdm import tqdm
import albumentations as A
from albumentations.pytorch.transforms import ToTensorV2
from augmentations.transforms import get_resize_augmentation
from augmentations.transforms import MEAN, STD

class VideoSet:
    def __init__(self, config, input_path):
        self.input_path = input_path # path to video file
        self.image_size = config.image_size
        self.transforms = A.Compose([
            get_resize_augmentation(config.image_size, keep_ratio=config.keep_ratio),
            A.Normalize(mean=MEAN, std=STD, max_pixel_value=1.0, p=1.0),
            ToTensorV2(p=1.0)
        ])

        self.initialize_stream()

    def initialize_stream(self):
        self.stream = cv2.VideoCapture(self.input_path)
        self.current_frame_id = 0
        self.video_info = {}

        if self.stream.isOpened(): 
            # get self.stream property 
            self.WIDTH  = int(self.stream.get(cv2.CAP_PROP_FRAME_WIDTH))   # float `width`
            self.HEIGHT = int(self.stream.get(cv2.CAP_PROP_FRAME_HEIGHT))  # float `height`
            self.FPS = int(self.stream.get(cv2.CAP_PROP_FPS))
            self.NUM_FRAMES = int(self.stream.get(cv2.CAP_PROP_FRAME_COUNT))
            self.video_info = {
                'name': os.path.basename(self.input_path),
                'width': self.WIDTH,
                'height': self.HEIGHT,
                'fps': self.FPS,
                'num_frames': self.NUM_FRAMES
            }
        else:
            raise f"Cannot read video {os.path.basename(self.input_path)}"

    def __getitem__(self, idx):
        success, ori_frame = self.stream.read()
        if not success:
            print(f"Cannot read frame {self.current_frame_id} from {self.video_info['name']}")
            return None
        else:
            self.current_frame_id = idx+1
        frame = cv2.cvtColor(ori_frame, cv2.COLOR_BGR2RGB).astype(np.float32)
        frame /= 255.0
        if self.transforms is not None:
            inputs = self.transforms(image=frame)['image']

        image_w, image_h = self.image_size
        ori_height, ori_width, _ = ori_frame.shape

        return {
            'img': inputs,
            'frame': self.current_frame_id,
            'ori_img': ori_frame,
            'image_ori_w': ori_width,
            'image_ori_h': ori_height,
            'image_w': image_w,
            'image_h': image_h,
        }

    def collate_fn(self, batch):
        batch = [x for x in batch if x is not None]
        if len(batch) == 0:
            return None

        imgs = torch.stack([s['img'] for s in batch])   
        ori_imgs = [s['ori_img'] for s in batch]
        frames = [s['frame'] for s in batch]
        image_ori_ws = [s['image_ori_w'] for s in batch]
        image_ori_hs = [s['image_ori_h'] for s in batch]
        image_ws = [s['image_w'] for s in batch]
        image_hs = [s['image_h'] for s in batch]
        img_scales = torch.tensor([1.0]*len(batch), dtype=torch.float)
        img_sizes = torch.tensor([imgs[0].shape[-2:]]*len(batch), dtype=torch.float)   

        return {
            'imgs': imgs,
            'frames': frames,
            'ori_imgs': ori_imgs,
            'image_ori_ws': image_ori_ws,
            'image_ori_hs': image_ori_hs,
            'image_ws': image_ws,
            'image_hs': image_hs,
            'img_sizes': img_sizes, 
            'img_scales': img_scales
        }

    def __len__(self):
        return self.NUM_FRAMES

    def __str__(self):
        s2 = f"Number of frames: {self.NUM_FRAMES}"
        return s2

class VideoLoader(DataLoader):
    def __init__(self, config, video_path):
        self.video_path = video_path
        dataset = VideoSet(config, video_path)
        self.video_info = dataset.video_info
       
        super(VideoLoader, self).__init__(
            dataset,
            batch_size= 1,
            num_workers=0,
            pin_memory=True,
            shuffle=False,
            collate_fn= dataset.collate_fn)

    def reinitialize_stream(self):
        self.dataset.initialize_stream()
        

class VideoWriter:
    def __init__(self, video_info, saved_path, obj_list):
        self.video_info = video_info
        self.saved_path = saved_path
        self.obj_list = obj_list

        video_name = self.video_info['name']
        outpath =os.path.join(self.saved_path, video_name)
        self.FPS = self.video_info['fps']
        self.WIDTH = self.video_info['width']
        self.HEIGHT = self.video_info['height']
        self.NUM_FRAMES = self.video_info['num_frames']
        self.outvid = cv2.VideoWriter(
            outpath,   
            cv2.VideoWriter_fourcc(*'mp4v'), 
            self.FPS, 
            (self.WIDTH, self.HEIGHT))

    def write(self, img, boxes, labels, scores=None, tracks=None):
        write_to_video(
            img, boxes, labels, 
            scores = scores,
            tracks=tracks, 
            imshow=False, 
            outvid = self.outvid, 
            obj_list=self.obj_list)

class VideoDetect:
    def __init__(self, args, config):
        self.device = torch.device("cuda" if torch.cuda.is_available() else 'cpu')   
        self.debug = args.debug
        self.config = config
        self.min_iou = args.min_iou
        self.min_conf = args.min_conf
        self.max_dets=config.max_post_nms
        self.keep_ratio=config.keep_ratio
        self.fusion_mode=config.fusion_mode

        if args.tta:
            self.tta = TTA(
                min_conf=args.tta_conf_threshold, 
                min_iou=args.tta_iou_threshold, 
                postprocess_mode=args.tta_ensemble_mode)
        else:
            self.tta = None

        if args.weight is not None:
            self.class_names, num_classes = get_class_names(args.weight)
        self.class_names.insert(0, 'Background')

        net = get_model(
            args, config,
            num_classes=num_classes)

        self.num_classes = num_classes
        self.model = Detector(model = net, device = self.device)
        self.model.eval()

        if args.weight is not None:                
            load_checkpoint(self.model, args.weight)

    def run(self, batch):
        with torch.no_grad():
            boxes_result = []
            labels_result = []
            scores_result = []
                
            if self.tta is not None:
                preds = self.tta.make_tta_predictions(self.model, batch)
            else:
                preds = self.model.inference_step(batch)

            for idx, outputs in enumerate(preds):
                img_w = batch['image_ws'][idx]
                img_h = batch['image_hs'][idx]
                img_ori_ws = batch['image_ori_ws'][idx]
                img_ori_hs = batch['image_ori_hs'][idx]
                
                outputs = postprocessing(
                    outputs, 
                    current_img_size=[img_w, img_h],
                    ori_img_size=[img_ori_ws, img_ori_hs],
                    min_iou=self.min_iou,
                    min_conf=self.min_conf,
                    max_dets=self.max_dets,
                    keep_ratio=self.keep_ratio,
                    output_format='xywh',
                    mode=self.fusion_mode)

                boxes = outputs['bboxes'] 

                # Label starts from 0
                labels = outputs['classes'] 
                scores = outputs['scores']

                boxes_result.append(boxes)
                labels_result.append(labels)
                scores_result.append(scores)

        return {
            "boxes": boxes_result, 
            "labels": labels_result,
            "scores": scores_result }


class Pipeline:
    def __init__(self, args, config):
        self.detector = VideoDetect(args, config)
        self.class_names = self.detector.class_names
        self.video_path = args.input_path
        self.saved_path = args.output_path
        self.videoloader = VideoLoader(config, self.video_path)
        
    def get_cam_name(self, path):
        filename = os.path.basename(path)
        cam_name = filename[:-4]
        return cam_name

    def run(self):
        
        cam_name = self.get_cam_name(self.video_path)
        videowriter = VideoWriter(
            self.videoloader.dataset.video_info,
            saved_path=self.saved_path,
            obj_list=self.class_names)
        

        obj_dict = {
            'frames': [],
            'labels': [],
            'boxes': []
        }

        for idx, batch in enumerate(tqdm(self.videoloader)):
            preds = self.detector.run(batch)
            ori_imgs = batch['ori_imgs']

            for i in range(len(ori_imgs)):
                boxes = preds['boxes'][i]
                labels = preds['labels'][i]
                scores = preds['scores'][i]
                frame_id = batch['frames'][i]

                ori_img = ori_imgs[i]
                track_result = self.tracker.run(ori_img, boxes, labels, scores)
                
                videowriter.write(
                    ori_img,
                    boxes = track_result['boxes'],
                    labels = track_result['labels'],
                    tracks = track_result['tracks'])

                for j in range(len(track_result['boxes'])):
                    obj_dict['frames'].append(frame_id)
                    obj_dict['tracks'].append(track_result['tracks'][j])
                    obj_dict['labels'].append(track_result['labels'][j])
                    obj_dict['boxes'].append(track_result['boxes'][j])

            
        return obj_dict