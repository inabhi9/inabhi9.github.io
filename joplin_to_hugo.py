#!/usr/bin/python3

import datetime
import json
import logging
import os
import pathlib
import re
import sys
from collections import deque

from getfilelistpy import getfilelist
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

FILE_TYPE = {'5': 'tag', '6': 'note_tag', '13': 'history', '2': 'dir', '1': 'content'}

L = logging.getLogger()


class JoplinParser:
    # holds value {TAG_ID: str}
    _tag = {}
    # holds value {NOTE_ID: TAG_ID[]}
    _note_tags = {}
    # holds value {filename: meta}
    _file_meta = {}
    # Image files used in a note should live in this directory under hugo project's static dir.
    RESOURCE_DIR_NAME = 'j-resources'
    # Only notes with this tag would be converted to Hugo format
    BLOG_TAG = 'blog'

    __IMAGE_REGEX = r'(\!\[.*\]\(:\/.*\))'

    def __init__(self, dirpath, outputdir):
        self._dir = dirpath
        self._outdir = outputdir

        self._prepare_output_dir()

    def scan(self):
        for file in self.files:
            meta = self._read_meta(file)
            self._file_meta[file] = meta

            method = getattr(self, '_parse_type%s_file' % meta['type_'], None)
            if method and method:
                method(file, meta)

        self._resolve_note_tag()

        for file in self.files:
            self._convert(file, self._file_meta[file])

    @property
    def files(self):
        """
        List absolute file path with extension .md

        :return generator:
        """
        for file in os.listdir(self._dir):
            abs_file = self._get_absolute_path(file)
            if os.path.isfile(abs_file) and file.endswith('.md'):
                yield abs_file

    def _get_absolute_path(self, filename):
        return '%s/%s' % (self._dir, filename)

    def _parse_type5_file(self, filepath, meta):
        assert meta['type_'] == '5'

        with open(filepath) as f:
            content = f.readline().strip()
            self._tag[meta['id']] = content
            return content

    def _parse_type6_file(self, filepath, meta):
        assert meta['type_'] == '6'

        note_id = meta['note_id']
        tag_id = meta['tag_id']
        self._note_tags[note_id] = self._note_tags.get(note_id) or []
        self._note_tags[note_id].append(tag_id)

    def _convert(self, filepath, meta):
        output_file = self._outdir + '/' + os.path.basename(filepath)
        tags = self._note_tags.get(meta['id']) or []
        if self.BLOG_TAG not in tags:
            L.info('File is not marked as blog: %s', filepath)
            return

        with open(output_file, 'w+') as o:
            with open(filepath) as f:
                line = f.readline()

                if not line:
                    L.warning('Title is missing in file. Skipping.: %s' % filepath)
                    return

                # Writing header
                deque(map(lambda x: o.writelines([x + '\n']), self.__get_hugo_header(line, meta)))

                while line != '':
                    line = f.readline()

                    if self.__is_image_syntax(line):
                        line = self.__replace_image(line)

                    o.writelines([line])

                    # These lines contains meta information
                    if f.tell() > meta['_pos']:
                        break

    def _prepare_output_dir(self):
        try:
            pathlib.Path(self._outdir + '/' + self.RESOURCE_DIR_NAME).mkdir(parents=True,
                                                                            exist_ok=True)
        except FileExistsError:
            L.warning('Output directory is not empty.')
            pass

    def _resolve_note_tag(self):
        note_tags = {id_: list(map(lambda x: self._tag[x], t_ids))
                     for id_, t_ids in self._note_tags.items()}

        self._note_tags = note_tags

    @classmethod
    def _read_meta(cls, filepath):
        """
        Reads content's meta information.

        :param str filepath: Absolute file path
        :return dict:
        """
        meta = {}
        with open(filepath, 'rb') as f:
            f.seek(0, os.SEEK_END)
            buf = bytearray()
            curr_pos = f.tell()

            while curr_pos >= 0:
                f.seek(curr_pos)
                b = f.read(1)
                curr_pos -= 1

                if b == b'\n' or b == b'\r':
                    line = buf.decode()[::-1]
                    key, value = line.split(':', 1)
                    meta[key.strip()] = value.strip()
                    # It should hold only content for a line.
                    buf = bytearray()
                    # Until we hit id: line, we're not interested in content
                    if line.startswith('id: '):
                        break
                else:
                    buf.extend(b)

            # Flushing first line if exists
            if len(buf) > 0:
                line = buf.decode()[::-1]
                key, value = line.split(':')
                meta[key.strip()] = value.strip()

        meta['_pos'] = curr_pos

        return cls.__format_meta(meta)

    @staticmethod
    def __format_meta(meta):
        def rz(x):
            return x.replace('Z', '+00:00')

        meta['created_time'] = datetime.datetime.fromisoformat(rz(meta['created_time']))
        meta['updated_time'] = datetime.datetime.fromisoformat(rz(meta['updated_time']))
        try:
            meta['user_created_time'] = datetime.datetime.fromisoformat(
                rz(meta['user_created_time']))
            meta['user_updated_time'] = datetime.datetime.fromisoformat(
                rz(meta['user_updated_time']))
        except KeyError:
            pass

        return meta

    def __is_image_syntax(self, s):
        g = re.search(self.__IMAGE_REGEX, s, re.IGNORECASE)

        return True if g else False

    def __replace_image(self, s: str):
        groups = re.search(self.__IMAGE_REGEX, s, re.IGNORECASE).groups()

        for group in groups:
            new_s = group.replace(':/', '/%s/' % self.RESOURCE_DIR_NAME)
            s = s.replace(group, new_s)

        return s

    def __get_hugo_header(self, title, meta):
        """
        Hugo post to a given file handler

        :param meta:
        :return list:
        """
        tags = self._note_tags.get(meta['id']) or []
        tags = list(filter(lambda x: x != self.BLOG_TAG, tags))

        return [
            '---',
            'title: "%s"' % title.strip(),
            'date: %s' % meta['created_time'].isoformat(),
            'draft: false',
            'tags: %s' % tags,
            '---'
        ]


class GDriveSource:
    # Joplin resource directory name
    J_RES_DIR_NAME = '.resource'

    def __init__(self, service_acc_info, folder_id, output_dir):
        self.service_acc_info = json.loads(service_acc_info)
        self.folder_id = folder_id
        self._outdir = output_dir

    @property
    def _drv_srv(self):
        api = 'drive'
        version = 'v3'

        return build(api, version, credentials=self.gd_credential)

    @property
    def gd_credential(self):
        scopes = ['https://www.googleapis.com/auth/drive']
        return service_account.Credentials.from_service_account_info(self.service_acc_info,
                                                                     scopes=scopes)

    def pull(self):
        self.__prepare_output_dir()
        L.info('Fetching file list.')
        res = getfilelist.GetFileList(self.resource)
        root_folder_id = self.__get_root_folder_id(res)
        res_folder_id = self.__get_resource_folder_id(res)

        self._pull_files(res, [root_folder_id])
        self._pull_files(res, [root_folder_id, res_folder_id], '%s/' % self.J_RES_DIR_NAME)

    def _pull_files(self, res, filter_folder_tree, filename_prefix=''):
        file_lists = res['fileList']

        for file_list in file_lists:
            folder_tree = file_list['folderTree']
            files = file_list['files']

            # Not a file from root; ie. from sub directory.
            if folder_tree != filter_folder_tree:
                continue

            for file in files:
                id_ = file['id']
                name = file['name']
                self.__download_file(id_, '%s%s' % (filename_prefix, name))

    @property
    def resource(self):
        return {
            "service_account": self.gd_credential,
            "id": self.folder_id,
            "fields": "files(name,id)",
        }

    def __get_resource_folder_id(self, res):
        folder_tree = res['folderTree']
        names = folder_tree['names']
        folder_ids = folder_tree['folders']
        resource_folder_name = self.J_RES_DIR_NAME
        resource_folder_idx = names.index(resource_folder_name)
        resource_folder_id = folder_ids[resource_folder_idx]

        return resource_folder_id

    def __get_root_folder_id(self, res):
        return res['folderTree']['folders'][0]

    def __get_abs_output_file_path(self, filename):
        return '%s/%s' % (self._outdir, filename)

    def __download_file(self, id_, name):
        output = self.__get_abs_output_file_path(name)
        request = self._drv_srv.files().get_media(fileId=id_)
        with open(output, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                L.debug('Download %d%%.' % int(status.progress() * 100))

        L.info('Download: %s', output)

    def __prepare_output_dir(self):
        pathlib.Path(self._outdir + '/%s' % self.J_RES_DIR_NAME).mkdir(parents=True, exist_ok=True)


if __name__ == '__main__':
    gds = GDriveSource(os.environ.get('GD_SERVICE_ACC_INFO'),
                       os.environ.get('GD_FOLDER_ID'),
                       sys.argv[1])
    gds.pull()
    L.info('Downloaded all the files.')

    in_dir, out_dir = sys.argv[1], sys.argv[2]
    jp = JoplinParser(in_dir, out_dir)
    jp.scan()

    L.info('Conversion completed.')