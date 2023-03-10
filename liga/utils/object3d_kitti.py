import numpy as np


def get_objects_from_label(label_file, cameras):
    with open(label_file, 'r') as f:
        lines = f.readlines()
    objects = [Object3d(line, cameras) for line in lines]
    return objects


def cls_type_to_id(cls_type):
    type_to_id = {'Car': 1, 'Van':2, 'Pedestrian': 3, 'Cyclist': 4, 'Trafficcone': 5, 'Others': 6}
    assert cls_type in type_to_id.keys(), 'cls_type not in type_to_id.keys()'
    # if cls_type not in type_to_id.keys():
    #     return -1
    return type_to_id[cls_type]


class Object3d(object):
    def __init__(self, line, cameras):
        label = line.strip().split(' ')
        self.src = line
        self.cls_type = label[0]
        self.cls_id = cls_type_to_id(self.cls_type)
        self.truncation = float(label[1])
        self.occlusion = float(label[2])  # 0:fully visible 1:partly occluded 2:largely occluded 3:unknown
        self.alpha = float(label[3])
        # self.box2d = np.array((float(label[11]), float(label[12]), float(label[13]), float(label[14])), dtype=np.float32)
        self.h = float(label[4])
        self.w = float(label[5])
        self.l = float(label[6])
        self.loc = np.array((float(label[7]), float(label[8]), float(label[9])), dtype=np.float32)
        self.dis_to_cam = np.linalg.norm(self.loc)
        self.ry = float(label[10])
        # self.score = float(label[15]) if label.__len__() == 16 else -1.0
        self.score = -1.0
        self.cameras = cameras
        # box2ds = {}
        # i = 11
        # for camera_id in cameras:
        #     box2ds['box2d' + str(camera_id)] = \
        #         np.array((float(label[i]), float(label[i+1]), float(label[i+2]), float(label[i+3])), dtype=np.float32)
            
        #     i += 4
        # self.__dict__.update(box2ds)
        self.level_str = None
        self.level = 1# self.get_kitti_obj_level()

    def get_kitti_obj_level(self):
        height = float(self.box2d[3]) - float(self.box2d[1]) + 1

        if height >= 40 and self.truncation <= 0.15 and self.occlusion <= 0:
            self.level_str = 'Easy'
            return 0  # Easy
        elif height >= 25 and self.truncation <= 0.3 and self.occlusion <= 1:
            self.level_str = 'Moderate'
            return 1  # Moderate
        elif height >= 25 and self.truncation <= 0.5 and self.occlusion <= 2:
            self.level_str = 'Hard'
            return 2  # Hard
        else:
            self.level_str = 'UnKnown'
            return -1

    def generate_corners3d(self):
        """
        generate corners3d representation for this object
        :return corners_3d: (8, 3) corners of box3d in camera coord
        """
        l, h, w = self.l, self.h, self.w
        x_corners = [l / 2, l / 2, -l / 2, -l / 2, l / 2, l / 2, -l / 2, -l / 2]
        y_corners = [0, 0, 0, 0, -h, -h, -h, -h]
        z_corners = [w / 2, -w / 2, -w / 2, w / 2, w / 2, -w / 2, -w / 2, w / 2]

        R = np.array([[np.cos(self.ry), 0, np.sin(self.ry)],
                      [0, 1, 0],
                      [-np.sin(self.ry), 0, np.cos(self.ry)]])
        corners3d = np.vstack([x_corners, y_corners, z_corners])  # (3, 8)
        corners3d = np.dot(R, corners3d).T
        corners3d = corners3d + self.loc
        return corners3d

    def to_str(self):
        print_str = '%s %.3f %.3f %.3f box2d: %s hwl: [%.3f %.3f %.3f] pos: %s ry: %.3f' \
                     % (self.cls_type, self.truncation, self.occlusion, self.alpha, self.box2d, self.h, self.w, self.l,
                        self.loc, self.ry)
        return print_str

    def to_kitti_format(self):
        kitti_str = '%s %.2f %d %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f' \
                    % (self.cls_type, self.truncation, int(self.occlusion), self.alpha, self.box2d[0], self.box2d[1],
                       self.box2d[2], self.box2d[3], self.h, self.w, self.l, self.loc[0], self.loc[1], self.loc[2],
                       self.ry)
        return kitti_str
