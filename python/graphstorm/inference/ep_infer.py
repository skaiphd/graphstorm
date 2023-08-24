"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Infer wrapper for edge classification and regression.
"""
import time
from dgl.distributed import DistTensor

from .graphstorm_infer import GSInfer
from ..model.utils import save_embeddings as save_gsgnn_embeddings
from ..model.utils import save_prediction_results
from ..model.utils import shuffle_predict
from ..model.gnn import do_full_graph_inference
from ..model.edge_gnn import edge_mini_batch_predict

from ..utils import sys_tracker, get_world_size, barrier

class GSgnnEdgePredictionInfer(GSInfer):
    """ Edge classification/regression infer.

    This is a highlevel infer wrapper that can be used directly
    to do edge classification/regression model inference.

    Parameters
    ----------
    model : GSgnnNodeModel
        The GNN model for node prediction.
    rank : int
        The rank.
    """

    def infer(self, loader, save_embed_path, save_prediction_path=None,
            use_mini_batch_infer=False, # pylint: disable=unused-argument
            node_id_mapping_file=None,
            edge_id_mapping_file=None,
            return_proba=True):
        """ Do inference

        The infer can do three things:
        1. (Optional) Evaluate the model performance on a test set if given
        2. Generate node embeddings

        Parameters
        ----------
        loader : GSEdgeDataLoader
            The mini-batch sampler for edge prediction task.
        save_embed_path : str
            The path where the GNN embeddings will be saved.
        save_prediction_path : str
            The path where the prediction results will be saved.
        use_mini_batch_infer : bool
            Whether or not to use mini-batch inference.
        node_id_mapping_file: str
            Path to the file storing node id mapping generated by the
            graph partition algorithm.
        return_proba: bool
            Whether to return all the predictions or the maximum prediction.
        """
        do_eval = self.evaluator is not None
        if do_eval:
            assert loader.data.labels is not None, \
                "A label field must be provided for edge classification " \
                "or regression inference when evaluation is required."

        sys_tracker.check('start inferencing')
        self._model.eval()
        embs = do_full_graph_inference(self._model, loader.data, fanout=loader.fanout,
                                       task_tracker=self.task_tracker)
        sys_tracker.check('compute embeddings')
        res = edge_mini_batch_predict(self._model, embs, loader, return_proba,
                                      return_label=do_eval)
        pred = res[0]
        label = res[1] if do_eval else None
        sys_tracker.check('compute prediction')

        # Only save the embeddings related to target edge types.
        infer_data = loader.data
        # TODO support multiple etypes
        assert len(infer_data.eval_etypes) == 1, \
            "GraphStorm only support single target edge type for training and inference"

        # do evaluation first
        if do_eval:
            test_start = time.time()
            val_score, test_score = self.evaluator.evaluate(pred, pred, label, label, 0)
            sys_tracker.check('run evaluation')
            if self.rank == 0:
                self.log_print_metrics(val_score=val_score,
                                       test_score=test_score,
                                       dur_eval=time.time() - test_start,
                                       total_steps=0)
        device = self.device
        if save_embed_path is not None:
            target_ntypes = set()
            for etype in infer_data.eval_etypes:
                target_ntypes.add(etype[0])
                target_ntypes.add(etype[2])

            # The order of the ntypes must be sorted
            embs = {ntype: embs[ntype] for ntype in sorted(target_ntypes)}
            save_gsgnn_embeddings(save_embed_path, embs, self.rank,
                get_world_size(),
                device=device,
                node_id_mapping_file=node_id_mapping_file)
        barrier()
        sys_tracker.check('save embeddings')

        if save_prediction_path is not None:
            if edge_id_mapping_file is not None:
                g = loader.data.g
                etype = infer_data.eval_etypes[0]
                pred_shape = list(pred.shape)
                pred_shape[0] = g.num_edges(etype)
                pred_data = DistTensor(pred_shape,
                    dtype=pred.dtype, name='predict-'+'-'.join(etype),
                    part_policy=g.get_edge_partition_policy(etype),
                    # TODO: this makes the tensor persistent in memory.
                    persistent=True)
                # edges that have predictions may be just a subset of the
                # entire edge set.
                pred_data[loader.target_eidx[etype]] = pred.cpu()

                pred = shuffle_predict(pred_data, edge_id_mapping_file, etype, self.rank,
                    get_world_size(), device=device)
            save_prediction_results(pred, save_prediction_path, self.rank)
        barrier()
        sys_tracker.check('save predictions')
