import os
import torch
import torch.nn as nn
import torch.distributed as dist

from liga.utils import nms_utils
from liga.utils.common_utils import create_logger
from liga.ops.iou3d_nms import iou3d_nms_utils
from liga.models.simple import backbone, gridsample, voxel_feature, detec_head_3d, map_encoder
from liga.models.simple import dense_heads


class StereoDetector3DTemplate(nn.Module):
    def __init__(self, model_cfg, num_class, dataset):
        super().__init__()
        self.model_cfg = model_cfg
        self.num_class = num_class
        self.dataset = dataset
        self.class_names = dataset.class_names
        self.register_buffer('global_step', torch.LongTensor(1).zero_())
        
        if model_cfg.get('MAP_ENCODER', None) is None:
            self.module_topology = [
            # 'backbone', 'gridsample','voxel_feature', 'dense_head_2d', 'dense_head'
            'backbone', 'gridsample','voxel_feature', 'dense_head'
        ]
        else:
            self.module_topology = [
            'map_encoder', 'voxel_feature', 'dense_head_2d', 'dense_head'
            # 'backbone', 'gridsample', 'map_encoder','voxel_feature', 'dense_head_2d', 'dense_head'
        ]

    def train(self, mode=True):
        self.training = mode
        for module in self.children():
            if module in self.model_info_dict['fixed_module_list']:
                module.eval()
            else:
                module.train(mode)
        return self

    @property
    def mode(self):
        return 'TRAIN' if self.training else 'TEST'

    def update_global_step(self):
        self.global_step += 1

    def build_networks(self):
        model_info_dict = {
            'module_list': [],
            'fixed_module_list': [],
            'grid_size': getattr(self.dataset, 'stereo_grid_size', self.dataset.grid_size),
            'point_cloud_range': self.dataset.point_cloud_range,
            'voxel_size': getattr(self.dataset, 'stereo_voxel_size', self.dataset.voxel_size),
            'boxes_gt_in_cam2_view': self.dataset.boxes_gt_in_cam2_view
        }
        for module_name in self.module_topology:
            module, model_info_dict = getattr(self, 'build_%s' % module_name)(
                model_info_dict=model_info_dict
            )
            self.add_module(module_name, module)
        self.model_info_dict = model_info_dict
        return model_info_dict['module_list']

    def build_backbone(self, model_info_dict):
        if self.model_cfg.get('BACKBONE', None) is None:
            return None, model_info_dict

        backbone_module = backbone.BackBoneSimple(model_cfg=self.model_cfg.BACKBONE)

        model_info_dict['module_list'].append(backbone_module)

        return backbone_module, model_info_dict

    def build_gridsample(self, model_info_dict):
        if self.model_cfg.get('GRIDSAMPLE', None) is None:
            return None, model_info_dict

        gridsample_module = gridsample.GridSampleSimple(model_cfg=self.model_cfg.GRIDSAMPLE,
                                                        grid_size=model_info_dict['grid_size'],
                                                        voxel_size=model_info_dict['voxel_size'],
                                                        point_cloud_range=model_info_dict['point_cloud_range'])
        model_info_dict['module_list'].append(gridsample_module)

        return gridsample_module, model_info_dict

    def build_map_encoder(self, model_info_dict):
        if self.model_cfg.get('MAP_ENCODER', None) is None:
            return None, model_info_dict

        map_encoder_module = map_encoder.MapEncoder(model_cfg=self.model_cfg.MAP_ENCODER)
        model_info_dict['module_list'].append(map_encoder_module)

        return map_encoder_module, model_info_dict

    def build_voxel_feature(self, model_info_dict):
        if self.model_cfg.get('VOXEL_FEATURE', None) is None:
            return None, model_info_dict

        voxel_feature_module = voxel_feature.VoxelFeatureSimple(model_cfg=self.model_cfg.VOXEL_FEATURE)

        model_info_dict['module_list'].append(voxel_feature_module)

        return voxel_feature_module, model_info_dict

    def build_dense_head_2d(self, model_info_dict):
        if self.model_cfg.get('DENSE_HEAD_2D', None) is None:
            return None, model_info_dict
        if self.model_cfg.DENSE_HEAD_2D.NAME == 'MMDet2DHead':
            dense_head_module = dense_heads.__all__[self.model_cfg.DENSE_HEAD_2D.NAME](
                model_cfg=self.model_cfg.DENSE_HEAD_2D
            )
            model_info_dict['module_list'].append(dense_head_module)
            return dense_head_module, model_info_dict
        else:
            dense_head_module = dense_heads.__all__[self.model_cfg.DENSE_HEAD_2D.NAME](
                model_cfg=self.model_cfg.DENSE_HEAD_2D,
                input_channels=32,
                num_class=self.num_class,
                class_names=self.class_names,
                grid_size=model_info_dict['grid_size'],
                point_cloud_range=model_info_dict['point_cloud_range'],
                predict_boxes_when_training=self.model_cfg.get('ROI_HEAD', False)
            )
            model_info_dict['module_list'].append(dense_head_module)
            return dense_head_module, model_info_dict

    def build_dense_head(self, model_info_dict):
        if self.model_cfg.get('DENSE_HEAD', None) is None:
            return None, model_info_dict
        dense_head_module = dense_heads.__all__[self.model_cfg.DENSE_HEAD.NAME](
            model_cfg=self.model_cfg.DENSE_HEAD,
            input_channels=self.model_cfg.DENSE_HEAD.num_channels,
            num_class=self.num_class if not self.model_cfg.DENSE_HEAD.CLASS_AGNOSTIC else 1,
            class_names=self.class_names,
            grid_size=model_info_dict['grid_size'],
            point_cloud_range=model_info_dict['point_cloud_range'],
            predict_boxes_when_training=self.model_cfg.get('ROI_HEAD', False) or self.model_cfg.DENSE_HEAD.get('predict_boxes_when_training', False)
        )
        model_info_dict['module_list'].append(dense_head_module)
        return dense_head_module, model_info_dict

    def forward(self, **kwargs):
        raise NotImplementedError

    def post_processing(self, batch_dict):
        """
        Args:
            batch_dict:
                batch_size:
                batch_cls_preds: (B, num_boxes, num_classes | 1) or (
                    N1+N2+..., num_classes | 1)
                                or [(B, num_boxes, num_class1), (B, num_boxes, num_class2) ...]
                multihead_label_mapping: [(num_class1), (num_class2), ...]
                batch_box_preds: (B, num_boxes, 7+C) or (N1+N2+..., 7+C)
                cls_preds_normalized: indicate whether batch_cls_preds is normalized
                batch_index: optional (N1+N2+...)
                has_class_labels: True/False
                roi_labels: (B, num_rois)  1 .. num_classes
                batch_pred_labels: (B, num_boxes, 1)
        Returns:

        """
        post_process_cfg = self.model_cfg.POST_PROCESSING
        batch_size = batch_dict['batch_size']
        recall_dict = {}
        pred_dicts = []
        for index in range(batch_size):
            if batch_dict.get('batch_index', None) is not None:
                assert batch_dict['batch_box_preds'].shape.__len__() == 2
                batch_mask = (batch_dict['batch_index'] == index)
            else:
                assert batch_dict['batch_box_preds'].shape.__len__() == 3
                batch_mask = index

            box_preds = batch_dict['batch_box_preds'][batch_mask]
            src_box_preds = box_preds

            if not isinstance(batch_dict['batch_cls_preds'], list):
                cls_preds = batch_dict['batch_cls_preds'][batch_mask]

                src_cls_preds = cls_preds
                assert cls_preds.shape[1] in [1, self.num_class]

                if not batch_dict['cls_preds_normalized']:
                    cls_preds = torch.sigmoid(cls_preds)
            else:
                cls_preds = [x[batch_mask]
                             for x in batch_dict['batch_cls_preds']]
                src_cls_preds = cls_preds
                if not batch_dict['cls_preds_normalized']:
                    cls_preds = [torch.sigmoid(x) for x in cls_preds]

            '''add by me'''
            # if 'toonnx' in batch_dict.keys():
            #     record_dict = {
            #         'cls_preds': cls_preds,
            #         'box_preds': box_preds,
            #     }
            #     pred_dicts.append(record_dict)
            #     continue
            '''add by me'''

            if post_process_cfg.NMS_CONFIG.MULTI_CLASSES_NMS:
                if batch_dict.get('has_class_labels', False):
                    label_key = 'roi_labels' if 'roi_labels' in batch_dict else 'batch_pred_labels'
                    label_preds = batch_dict[label_key][index]
                else:
                    label_preds = None
                if 'toonnx' in batch_dict.keys():
                    pred_scores, pred_labels, pred_boxes = nms_utils.multi_classes_nms(
                        cls_scores=cls_preds, box_preds=box_preds,
                        nms_config=post_process_cfg.NMS_CONFIG,
                        score_thresh=0,
                        label_preds=label_preds,
                        toonnx = True
                    )
                else:
                    pred_scores, pred_labels, pred_boxes = nms_utils.multi_classes_nms(
                        cls_scores=cls_preds, box_preds=box_preds,
                        nms_config=post_process_cfg.NMS_CONFIG,
                        score_thresh=post_process_cfg.SCORE_THRESH,
                        label_preds=label_preds,
                    )
                final_scores = pred_scores
                #final_labels = pred_labels + 1
                final_labels = pred_labels if 'toonnx' in batch_dict.keys() else pred_labels + 1 #changed by me  to onnx needed
                final_boxes = pred_boxes
            else:
                cls_preds, label_preds = torch.max(cls_preds, dim=-1)
                if batch_dict.get('has_class_labels', False):
                    label_key = 'roi_labels' if 'roi_labels' in batch_dict else 'batch_pred_labels'
                    label_preds = batch_dict[label_key][index]
                else:
                    label_preds = label_preds + 1
                selected, selected_scores = nms_utils.class_agnostic_nms(
                    box_scores=cls_preds, box_preds=box_preds,
                    nms_config=post_process_cfg.NMS_CONFIG,
                    score_thresh=post_process_cfg.SCORE_THRESH
                )

                if post_process_cfg.OUTPUT_RAW_SCORE:
                    max_cls_preds, _ = torch.max(src_cls_preds, dim=-1)
                    selected_scores = max_cls_preds[selected]

                final_scores = selected_scores
                final_labels = label_preds[selected]
                final_boxes = box_preds[selected]

            record_dict = {
                'pred_boxes': final_boxes,
                'pred_scores': final_scores,
                'pred_labels': final_labels,
            }

            # recall_dict, iou_results, ioubev_results = self.generate_recall_record(
            #     box_preds=final_boxes if 'rois' not in batch_dict else src_box_preds,
            #     recall_dict=recall_dict, batch_index=index, data_dict=batch_dict,
            #     thresh_list=post_process_cfg.RECALL_THRESH_LIST
            # )
            # if iou_results is not None:
            #     record_dict['iou_results'] = iou_results
            #     record_dict['ioubev_results'] = ioubev_results

            pred_dicts.append(record_dict)

        # return pred_dicts, recall_dict
        return pred_dicts, {}

    @staticmethod
    def generate_recall_record(box_preds, recall_dict, batch_index, data_dict=None, thresh_list=None):
        if 'gt_boxes' not in data_dict:
            return recall_dict, None, None

        rois = data_dict['rois'][batch_index] if 'rois' in data_dict else None
        gt_boxes = data_dict['gt_boxes'][batch_index]

        if recall_dict.__len__() == 0:
            recall_dict = {'gt': 0}
            for cur_thresh in thresh_list:
                recall_dict['roi_%s' % (str(cur_thresh))] = 0
                recall_dict['rcnn_%s' % (str(cur_thresh))] = 0

        cur_gt = gt_boxes
        k = cur_gt.__len__() - 1
        while k > 0 and cur_gt[k].sum() == 0:
            k -= 1
        cur_gt = cur_gt[:k + 1]

        if cur_gt.shape[0] > 0:
            if box_preds.shape[0] > 0:
                iou3d_rcnn = iou3d_nms_utils.boxes_iou3d_gpu(
                    box_preds[:, 0:7], cur_gt[:, 0:7])
                ioubev_rcnn = iou3d_nms_utils.boxes_iou_bev(
                    box_preds[:, 0:7], cur_gt[:, 0:7])
            else:
                iou3d_rcnn = torch.zeros((0, cur_gt.shape[0]))
                ioubev_rcnn = torch.zeros((0, cur_gt.shape[00]))

            if rois is not None:
                iou3d_roi = iou3d_nms_utils.boxes_iou3d_gpu(
                    rois[:, 0:7], cur_gt[:, 0:7])

            for cur_thresh in thresh_list:
                if iou3d_rcnn.shape[0] == 0:
                    recall_dict['rcnn_%s' % str(cur_thresh)] += 0
                else:
                    rcnn_recalled = (iou3d_rcnn.max(dim=0)[
                                     0] > cur_thresh).sum().item()
                    recall_dict['rcnn_%s' % str(cur_thresh)] += rcnn_recalled
                if rois is not None:
                    roi_recalled = (iou3d_roi.max(dim=0)[
                                    0] > cur_thresh).sum().item()
                    recall_dict['roi_%s' % str(cur_thresh)] += roi_recalled

            # per box iou
            if iou3d_rcnn.shape[0] == 0:
                iou_results = [0.] * cur_gt.shape[0]
                ioubev_results = [0.] * cur_gt.shape[0]
            else:
                iou_results = iou3d_rcnn.max(0).values.cpu().numpy().tolist()
                ioubev_results = ioubev_rcnn.max(0).values.cpu().numpy().tolist()

            recall_dict['gt'] += cur_gt.shape[0]
        else:
            gt_iou = box_preds.new_zeros(box_preds.shape[0])
            iou_results = []
            ioubev_results = []
        return recall_dict, iou_results, ioubev_results

    def load_params_from_file(self, filename, logger, to_cpu=False):
        if not os.path.isfile(filename):
            raise FileNotFoundError
        if logger is not None:
            logger.info('==> Loading parameters from checkpoint %s to %s' %
                        (filename, 'CPU' if to_cpu else 'GPU'))

        loc_type = torch.device('cpu') if to_cpu else None
        checkpoint = torch.load(filename, map_location=loc_type)
        model_state_disk = checkpoint['model_state']

        if 'version' in checkpoint:
            if logger is not None:
                logger.info('==> Checkpoint trained from version: %s' %
                            checkpoint['version'])

        update_model_state = {}
        for key, val in model_state_disk.items():
            if key in self.state_dict() and self.state_dict()[key].shape == model_state_disk[key].shape:
                update_model_state[key] = val
                # logger.info('Update weight %s: %s' % (key, str(val.shape)))

        state_dict = self.state_dict()
        state_dict.update(update_model_state)
        self.load_state_dict(state_dict)

        for key in state_dict:
            if key not in update_model_state:
                if logger is not None:
                    logger.info('Not updated weight %s: %s <- %s' %
                                (key, str(state_dict[key].shape), str(model_state_disk[key].shape) if key in model_state_disk else 'None'))

        if logger is not None:
            logger.info('==> Done (loaded %d/%d)' %
                        (len(update_model_state), len(self.state_dict())))

    def load_params_with_optimizer(self, filename, to_cpu=False, optimizer=None, logger=None):
        if not os.path.isfile(filename):
            raise FileNotFoundError
        if logger is not None:
            logger.info('==> Loading parameters from checkpoint %s to %s' %
                        (filename, 'CPU' if to_cpu else 'GPU'))
        loc_type = torch.device('cpu') if to_cpu else None
        checkpoint = torch.load(filename, map_location=loc_type)
        epoch = checkpoint.get('epoch', -1)
        it = checkpoint.get('it', 0.0)

        self.load_state_dict(checkpoint['model_state'])

        if optimizer is not None:
            if 'optimizer_state' in checkpoint and checkpoint['optimizer_state'] is not None:
                if logger is not None:
                    logger.info('==> Loading optimizer parameters from checkpoint %s to %s'
                                % (filename, 'CPU' if to_cpu else 'GPU'))
                optimizer.load_state_dict(checkpoint['optimizer_state'])
            else:
                assert filename[-4] == '.', filename
                src_file, ext = filename[:-4], filename[-3:]
                optimizer_filename = '%s_optim.%s' % (src_file, ext)
                if os.path.exists(optimizer_filename):
                    optimizer_ckpt = torch.load(
                        optimizer_filename, map_location=loc_type)
                    optimizer.load_state_dict(
                        optimizer_ckpt['optimizer_state'])

        if 'version' in checkpoint:
            print('==> Checkpoint trained from version: %s' %
                  checkpoint['version'])
        if logger is not None:
            logger.info('==> Done')

        return it, epoch